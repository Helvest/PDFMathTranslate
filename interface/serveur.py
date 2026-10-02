#!/usr/bin/env python3
"""Serveur de l'interface de traduction.

API locale pour piloter les projets : lister, creer, editer les fichiers,
lancer les etapes. L'interface HTML est servie sur la meme origine.

    python serveur.py            -> http://127.0.0.1:8756

Aucune dependance a ajouter : FastAPI et uvicorn sont deja installes (via
Gradio). Aucune base de donnees : un projet = un dossier.

Voir ../PLAN.md pour le plan complet.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

# ---------------------------------------------------------------- chemins

RACINE = Path(__file__).resolve().parent.parent
DEPOT = RACINE  # le serveur vit dans le depot (PDFMathTranslate/interface/)
PROJETS = RACINE.parent / "Projets"
INTERFACE = Path(__file__).resolve().parent

# Python et scripts du projet
PY = DEPOT / ".venv" / "Scripts" / "python.exe"
ANALYSER = DEPOT / "analyser.py"
TRADUIRE = DEPOT / "traduire.sh"

# Fichiers editables depuis l'interface, et leur emplacement dans le projet.
FICHIERS_EDITABLES = {
    "consignes-contexte": "analyse/consignes-contexte.md",
    "consignes-polices": "analyse/consignes-polices.md",
    "glossaire": "analyse/glossaire.csv",
    "polices": "analyse/polices.csv",
}

# Le contexte vit dans analyse/contexte/ : un .json par PDF, plus _lot.json.
DOSSIER_CONTEXTE = "analyse/contexte"

# Options de lancement, par projet. Ecrites dans <projet>/options.json.
OPTIONS_DEFAUT = {
    "lang_in": "en",
    "lang_out": "fr",
    "model": "inclusionai/ling-3.0-flash-sante:free",
    "term_model": "meituan/longcat-2.5-preview:free",
    "qps": 5,
    "term_qps": 5,
    "pool_max_workers": 5,
    "term_pool_max_workers": 1,
    "no_download": False,
    "ignorer_contexte": False,
    "glossaire_manuel": True,
    "pdfs_analyser": [],
    "pdfs_traduire": [],
}

DOSSIERS_PROJET = ("source", "traduits", "downloads", "analyse", "analyse/polices")

app = FastAPI(title="Traduction PDF")

# ---------------------------------------------------------------- etat

# Etat des etapes par projet. En memoire : le serveur redemarre rarement et
# un travail en cours ne survit pas a un redemarrage de toute facon.
#   {projet: {etape: {"etat":..., "fait":..., "total":..., "log":..., "debut":...}}}
ETATS: dict[str, dict[str, dict]] = {}
VERROU = threading.Lock()

# Regles de parallelisation (voir PLAN.md section 3).
# Une etape en cours interdit celles listees ici, POUR LE MEME PROJET.
CONFLITS = {
    "contexte": {"glossaire", "traduire"},
    "polices": {"glossaire", "traduire"},
    "glossaire": {"contexte", "polices", "traduire"},
    "traduire": {"contexte", "polices", "glossaire", "traduire"},
}

ETAPES = ("contexte", "polices", "glossaire", "traduire")

# "tout" enchaine les 4 etapes dans l'ordre, en un seul processus.
ETAPES_TOUT = ("contexte", "polices", "glossaire", "traduire")

RE_PROGRESS = re.compile(r"^\[PROGRESS\]\s+(\d+)/(\d+)\s*(.*)$")


# ---------------------------------------------------------------- securite

def _projet(nom: str) -> Path:
    """Chemin d'un projet, en refusant toute sortie de Projets/."""
    if not re.fullmatch(r"[A-Za-z0-9 _-]{1,64}", nom):
        raise HTTPException(400, "nom de projet invalide")
    p = (PROJETS / nom).resolve()
    if PROJETS.resolve() not in p.parents and p != PROJETS.resolve():
        raise HTTPException(400, "chemin hors de Projets/")
    return p


def _fichier(projet: Path, chemin: str) -> Path:
    """Chemin d'un fichier dans un projet, en refusant les remontees."""
    p = (projet / chemin).resolve()
    if projet.resolve() not in p.parents:
        raise HTTPException(400, "chemin hors du projet")
    return p


def _liste_projets() -> list[dict]:
    if not PROJETS.is_dir():
        return []
    out = []
    for d in sorted(PROJETS.iterdir()):
        if not d.is_dir():
            continue
        meta = d / "projet.json"
        infos = {"nom": d.name}
        if meta.is_file():
            try:
                infos.update(json.loads(meta.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                pass
        infos["nom"] = d.name  # le dossier fait foi
        src = d / "source"
        infos["pdfs"] = len(list(src.glob("*.pdf"))) if src.is_dir() else 0
        infos["etapes"] = _etat_projet(d.name)
        out.append(infos)
    return out


# ---------------------------------------------------------------- etapes

def _etat_projet(nom: str) -> dict:
    """Etat des 4 etapes, avec les blocages calcules."""
    with VERROU:
        etats = ETATS.get(nom, {})
        en_cours = {e for e, v in etats.items() if v.get("etat") == "en_cours"}

        resultat = {}
        for etape in ETAPES:
            v = etats.get(etape, {})
            etat = v.get("etat", "pret")

            # une autre etape en cours peut bloquer celle-ci
            if etat != "en_cours":
                bloquants = [
                    e for e in en_cours if etape in CONFLITS.get(e, set()) or e == etape
                ]
                if bloquants:
                    etat = "bloque"
                    v = {**v, "raison": f"{bloquants[0]} en cours"}

            resultat[etape] = {
                "etat": etat,
                "fait": v.get("fait", 0),
                "total": v.get("total", 0),
                "raison": v.get("raison", ""),
                "erreur": v.get("erreur", ""),
            }
        return resultat


def _suivre(projet: str, etape: str, proc: subprocess.Popen) -> None:
    """Lit la sortie du processus, met a jour l'etat et le log."""
    log: list[str] = []
    for ligne in proc.stdout:  # type: ignore[union-attr]
        ligne = ligne.rstrip("\n")
        log.append(ligne)

        m = RE_PROGRESS.match(ligne.strip())
        if m:
            with VERROU:
                ETATS[projet][etape]["fait"] = int(m.group(1))
                ETATS[projet][etape]["total"] = int(m.group(2))
                ETATS[projet][etape]["libelle"] = m.group(3).strip()

        # on garde les 400 dernieres lignes : assez pour le panneau de log
        if len(log) > 400:
            del log[:100]
        with VERROU:
            ETATS[projet][etape]["log"] = "\n".join(log)

    proc.wait()
    with VERROU:
        e = ETATS[projet][etape]
        e["etat"] = "termine" if proc.returncode == 0 else "erreur"
        e["fin"] = datetime.now().isoformat(timespec="seconds")
        if proc.returncode != 0:
            e["erreur"] = f"code {proc.returncode}"


def _options(projet: str) -> dict:
    """Options de lancement du projet, completes par les valeurs par defaut."""
    f = PROJETS / projet / "options.json"
    opts = dict(OPTIONS_DEFAUT)
    if f.is_file():
        try:
            opts.update(json.loads(f.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    return opts


def _demarrer(projet: str, etape: str) -> subprocess.Popen:
    """Construit la commande et lance le processus, SANS le suivre.

    Separe de _lancer pour que l'enchainement "tout" puisse attendre la fin
    de chaque etape avant de lancer la suivante.
    """
    dossier = PROJETS / projet
    log_path = dossier / "analyse" / f"{etape}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    o = _options(projet)

    if etape == "traduire":
        cmd = ["bash", str(TRADUIRE)]
        env = {
            **os.environ,
            "PDF2ZH_PROJET": projet,
            "PDF2ZH_LANG_IN": str(o["lang_in"]),
            "PDF2ZH_LANG_OUT": str(o["lang_out"]),
            "PDF2ZH_MODEL": str(o["model"]),
            "PDF2ZH_TERM_MODEL": str(o["term_model"]),
            "PDF2ZH_QPS": str(o["qps"]),
            "PDF2ZH_TERM_QPS": str(o["term_qps"]),
            "PDF2ZH_POOL": str(o["pool_max_workers"]),
            "PDF2ZH_TERM_POOL": str(o["term_pool_max_workers"]),
        }
        # la selection de PDFs passe en arguments positionnels
        if o.get("pdfs_traduire"):
            cmd += [str(dossier / "source" / n) for n in o["pdfs_traduire"]]
    else:
        cmd = [
            str(PY), str(ANALYSER),
            "--projet", projet,
            "--etapes", etape,
            "--lang-out", str(o["lang_out"]),
        ]
        if o.get("no_download"):
            cmd.append("--no-download")
        if o.get("ignorer_contexte"):
            cmd.append("--ignorer-contexte")
        if o.get("pdfs_analyser"):
            cmd += ["--pdfs", ",".join(o["pdfs_analyser"])]
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}

    return subprocess.Popen(
        cmd,
        cwd=str(DEPOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        bufsize=1,
    )


def _lancer(projet: str, etape: str) -> None:
    """Lance une etape en arriere-plan et la suit."""
    proc = _demarrer(projet, etape)
    threading.Thread(target=_suivre, args=(projet, etape, proc), daemon=True).start()


def _corbeille(chemin: Path) -> None:
    """Envoie un fichier a la corbeille. Jamais de suppression definitive.

    send2trash si disponible, sinon repli sur un dossier .corbeille horodate.
    """
    try:
        from send2trash import send2trash  # type: ignore

        send2trash(str(chemin))
    except ImportError:
        cible = PROJETS / ".corbeille" / f"{chemin.name}-{int(time.time())}"
        cible.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(chemin), str(cible))


# ---------------------------------------------------------------- API

@app.get("/api/projets")
def api_projets():
    return {"projets": _liste_projets()}


@app.post("/api/projets")
def api_creer(payload: dict):
    nom = (payload.get("nom") or "").strip()
    if not nom:
        raise HTTPException(400, "nom requis")
    p = _projet(nom)
    if p.exists():
        raise HTTPException(409, f"le projet '{nom}' existe deja")

    for d in DOSSIERS_PROJET:
        (p / d).mkdir(parents=True, exist_ok=True)

    (p / "projet.json").write_text(
        json.dumps(
            {
                "nom": nom,
                "cree": datetime.now().isoformat(timespec="seconds"),
                "lang_in": "en",
                "lang_out": "fr",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # les modeles de consignes, via le meme code que la ligne de commande
    subprocess.run(
        [str(PY), str(ANALYSER), "--projet", nom, "--creer", "--etapes", ""],
        cwd=str(DEPOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return {"ok": True, "nom": nom}


@app.delete("/api/projets/{nom}")
def api_supprimer(nom: str):
    p = _projet(nom)
    if not p.is_dir():
        raise HTTPException(404, "projet introuvable")
    _corbeille(p)
    return {"ok": True}


EXT_POLICE = (".ttf", ".otf", ".ttc")


def _polices(p: Path) -> dict:
    """Polices disponibles : installees, telechargees, deja proposees.

    On liste TOUT ce qui est utilisable, meme non installe, pour que
    l'utilisateur puisse choisir dans une liste deroulante.
    """
    installees = []
    d = p / "analyse" / "polices"
    if d.is_dir():
        installees = sorted(f.name for f in d.iterdir() if f.is_file())

    # polices presentes dans les archives deballees de downloads/
    telechargees = []
    dd = p / "downloads"
    if dd.is_dir():
        for f in sorted(dd.rglob("*")):
            if f.is_file() and f.suffix.lower() in EXT_POLICE:
                # chemin relatif a downloads/, pour rester lisible
                telechargees.append(str(f.relative_to(dd)).replace("\\", "/"))

    # ce que le CSV propose deja (colonne 'propose')
    proposees = []
    csv_path = p / "analyse" / "polices.csv"
    if csv_path.is_file():
        import csv as _csv

        try:
            with csv_path.open(encoding="utf-8-sig", newline="") as f:
                for row in _csv.DictReader(f):
                    v = (row.get("propose") or "").strip()
                    if v and v not in proposees:
                        proposees.append(v)
        except OSError:
            pass

    return {
        "installees": installees,
        "telechargees": telechargees,
        "proposees": proposees,
        "toutes": sorted(set(installees + proposees + [Path(t).name for t in telechargees])),
    }


def _contextes(p: Path) -> dict:
    """Un contexte JSON par PDF, plus le lot."""
    d = p / DOSSIER_CONTEXTE
    if not d.is_dir():
        return {"lot": None, "pdfs": []}
    out = []
    for f in sorted(d.glob("*.json")):
        if f.name.startswith("_"):
            continue
        try:
            c = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            c = {}
        out.append({
            "cle": f.stem,
            "fichier": c.get("fichier", f.stem + ".pdf"),
            "pages": len(c.get("pages", {})),
            "points": len(c.get("points", [])),
        })
    lot = (d / "_lot.json").is_file()
    return {"lot": lot, "pdfs": out}


@app.get("/api/projets/{nom}/options")
def api_lire_options(nom: str):
    p = _projet(nom)
    f = p / "options.json"
    opts = dict(OPTIONS_DEFAUT)
    if f.is_file():
        try:
            opts.update(json.loads(f.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    return opts


@app.put("/api/projets/{nom}/options")
def api_ecrire_options(nom: str, payload: dict):
    p = _projet(nom)
    if not p.is_dir():
        raise HTTPException(404, "projet introuvable")
    # on ne garde que les cles connues : evite les fichiers pourris
    opts = {k: payload.get(k, v) for k, v in OPTIONS_DEFAUT.items()}
    (p / "options.json").write_text(
        json.dumps(opts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {"ok": True, "options": opts}


@app.get("/api/projets/{nom}/contexte/{cle}")
def api_lire_contexte(nom: str, cle: str):
    """Lit un contexte JSON. cle = nom du PDF sans extension, ou _lot."""
    p = _projet(nom)
    f = _fichier(p, f"{DOSSIER_CONTEXTE}/{cle}.json")
    if not f.is_file():
        raise HTTPException(404, "contexte introuvable")
    try:
        return {"contenu": json.loads(f.read_text(encoding="utf-8"))}
    except (json.JSONDecodeError, OSError) as e:
        raise HTTPException(500, f"contexte illisible : {e}")


@app.put("/api/projets/{nom}/contexte/{cle}")
def api_ecrire_contexte(nom: str, cle: str, payload: dict):
    """Ecrit un contexte JSON. Le corps est l'objet complet."""
    p = _projet(nom)
    f = _fichier(p, f"{DOSSIER_CONTEXTE}/{cle}.json")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(
        json.dumps(payload.get("contenu", {}), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {"ok": True}


@app.get("/api/projets/{nom}")
def api_projet(nom: str):
    p = _projet(nom)
    if not p.is_dir():
        raise HTTPException(404, "projet introuvable")

    meta = {}
    if (p / "projet.json").is_file():
        try:
            meta = json.loads((p / "projet.json").read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    def _liste(dossier: str, motif: str) -> list[dict]:
        d = p / dossier
        if not d.is_dir():
            return []
        return [
            {
                "nom": f.name,
                "taille": f.stat().st_size,
                "modifie": datetime.fromtimestamp(f.stat().st_mtime).isoformat(
                    timespec="seconds"
                ),
            }
            for f in sorted(d.glob(motif))
            if f.is_file()
        ]

    return {
        "nom": nom,
        "meta": meta,
        "source": _liste("source", "*.pdf"),
        "traduits": _liste("traduits", "*.pdf"),
        "downloads": _liste("downloads", "*"),
        "polices_installees": _polices(p),
        "contextes": _contextes(p),
        "fichiers": {
            cle: (p / chemin).is_file() for cle, chemin in FICHIERS_EDITABLES.items()
        },
        "etapes": _etat_projet(nom),
    }


@app.get("/api/projets/{nom}/fichier/{cle}")
def api_lire_fichier(nom: str, cle: str):
    if cle not in FICHIERS_EDITABLES:
        raise HTTPException(400, "fichier inconnu")
    p = _projet(nom)
    f = _fichier(p, FICHIERS_EDITABLES[cle])
    if not f.is_file():
        return {"contenu": "", "existe": False}
    return {"contenu": f.read_text(encoding="utf-8-sig", errors="replace"), "existe": True}


@app.put("/api/projets/{nom}/fichier/{cle}")
def api_ecrire_fichier(nom: str, cle: str, payload: dict):
    if cle not in FICHIERS_EDITABLES:
        raise HTTPException(400, "fichier inconnu")
    p = _projet(nom)
    f = _fichier(p, FICHIERS_EDITABLES[cle])
    f.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig pour le CSV : Excel l'ouvre correctement
    enc = "utf-8-sig" if f.suffix == ".csv" else "utf-8"
    f.write_text(payload.get("contenu", ""), encoding=enc)
    return {"ok": True}


@app.post("/api/projets/{nom}/pdf")
async def api_upload(nom: str, fichier: UploadFile):
    p = _projet(nom)
    if not p.is_dir():
        raise HTTPException(404, "projet introuvable")
    if not (fichier.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "seuls les PDF sont acceptes")
    cible = _fichier(p, f"source/{Path(fichier.filename).name}")
    cible.write_bytes(await fichier.read())
    return {"ok": True, "nom": cible.name}


@app.delete("/api/projets/{nom}/pdf/{fichier}")
def api_supprimer_pdf(nom: str, fichier: str, type: str = "source"):
    """Supprime un PDF. Passe par la corbeille, jamais definitif."""
    p = _projet(nom)
    dossier = "traduits" if type == "traduit" else "source"
    f = _fichier(p, f"{dossier}/{fichier}")
    if not f.is_file():
        raise HTTPException(404, "PDF introuvable")
    _corbeille(f)
    return {"ok": True, "supprime": fichier}


@app.post("/api/projets/{nom}/pdf/supprimer-tout")
def api_supprimer_tous_pdf(nom: str, payload: dict):
    """Vide source/ (et traduits/ si demande). Tout part a la corbeille."""
    p = _projet(nom)
    dossiers = ["traduits"] if payload.get("traduits_seulement") else ["source"]
    if payload.get("avec_traduits") and "traduits" not in dossiers:
        dossiers.append("traduits")

    n = 0
    for d in dossiers:
        dossier = p / d
        if not dossier.is_dir():
            continue
        for f in dossier.glob("*.pdf"):
            _corbeille(f)
            n += 1
    return {"ok": True, "supprimes": n}


@app.get("/api/projets/{nom}/pdf/{fichier}")
def api_pdf(nom: str, fichier: str, type: str = "source"):
    p = _projet(nom)
    dossier = "traduits" if type == "traduit" else "source"
    f = _fichier(p, f"{dossier}/{fichier}")
    if not f.is_file():
        raise HTTPException(404, "PDF introuvable")
    return FileResponse(str(f), media_type="application/pdf")


@app.post("/api/projets/{nom}/etape/{etape}")
def api_lancer_etape(nom: str, etape: str):
    if etape not in ETAPES and etape != "tout":
        raise HTTPException(400, f"etape inconnue : {etape}")
    p = _projet(nom)
    if not p.is_dir():
        raise HTTPException(404, "projet introuvable")

    with VERROU:
        etats = ETATS.setdefault(nom, {})
        if etats.get(etape, {}).get("etat") == "en_cours":
            raise HTTPException(409, f"{etape} tourne deja")
        for autre, v in etats.items():
            if v.get("etat") == "en_cours" and (
                etape in CONFLITS.get(autre, set()) or autre == etape
            ):
                raise HTTPException(409, f"bloque : {autre} en cours")
        etats[etape] = {
            "etat": "en_cours",
            "fait": 0,
            "total": 0,
            "log": "",
            "debut": datetime.now().isoformat(timespec="seconds"),
        }

    if etape == "tout":
        # on enchaine dans un seul fil : chaque etape attend la precedente
        etats["tout"] = {
            "etat": "en_cours",
            "fait": 0,
            "total": 0,
            "log": "",
            "debut": datetime.now().isoformat(timespec="seconds"),
        }

        def enchainer() -> None:
            for e in ETAPES_TOUT:
                with VERROU:
                    etats[e] = {
                        "etat": "en_cours", "fait": 0, "total": 0,
                        "log": "", "debut": datetime.now().isoformat(timespec="seconds"),
                    }
                proc = _demarrer(nom, e)
                _suivre(nom, e, proc)
                with VERROU:
                    if etats[e].get("etat") == "erreur":
                        etats["tout"]["etat"] = "erreur"
                        etats["tout"]["erreur"] = f"{e} a echoue"
                        return
            with VERROU:
                etats["tout"]["etat"] = "termine"

        threading.Thread(target=enchainer, daemon=True).start()
        return {"ok": True, "etape": "tout"}

    _lancer(nom, etape)
    return {"ok": True, "etape": etape}


@app.get("/api/projets/{nom}/etape/{etape}/log")
def api_log(nom: str, etape: str):
    _projet(nom)
    with VERROU:
        v = ETATS.get(nom, {}).get(etape, {})
        return {
            "etat": v.get("etat", "pret"),
            "fait": v.get("fait", 0),
            "total": v.get("total", 0),
            "libelle": v.get("libelle", ""),
            "log": v.get("log", ""),
            "erreur": v.get("erreur", ""),
        }


@app.get("/api/etat")
def api_etat():
    return {
        "proxy": _proxy_actif(),
        "depot": str(DEPOT),
        "projets_dir": str(PROJETS),
    }


def _proxy_actif() -> bool:
    import urllib.request

    try:
        urllib.request.urlopen("http://127.0.0.1:8645/v1/models", timeout=3)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- interface

@app.get("/")
def racine():
    index = INTERFACE / "index.html"
    if not index.is_file():
        return JSONResponse(
            {"erreur": "interface/index.html absent", "api": "/docs"}, status_code=200
        )
    return FileResponse(str(index))


def main() -> int:
    if not DEPOT.is_dir():
        print(f"depot introuvable : {DEPOT}", file=sys.stderr)
        return 1
    PROJETS.mkdir(exist_ok=True)

    port = int(os.environ.get("PORT", "8756"))
    print(f"projets : {PROJETS}")
    print(f"proxy   : {'actif' if _proxy_actif() else 'INACTIF (les etapes LLM echoueront)'}")
    print(f"\n  http://127.0.0.1:{port}\n")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
