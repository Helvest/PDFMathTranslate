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
    "contexte": "analyse/contexte.md",
    "glossaire": "analyse/glossaire.csv",
    "polices": "analyse/polices.csv",
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


def _lancer(projet: str, etape: str) -> None:
    """Demarre une etape en arriere-plan."""
    dossier = PROJETS / projet
    log_path = dossier / "analyse" / f"{etape}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    if etape == "traduire":
        cmd = ["bash", str(TRADUIRE)]
        env = {**os.environ, "PDF2ZH_PROJET": projet}
    else:
        cmd = [str(PY), str(ANALYSER), "--projet", projet, "--etapes", etape]
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}

    proc = subprocess.Popen(
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
    threading.Thread(target=_suivre, args=(projet, etape, proc), daemon=True).start()


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
    # on ne supprime jamais sans filet : le dossier part a la corbeille
    try:
        from send2trash import send2trash  # type: ignore

        send2trash(str(p))
    except ImportError:
        # repli : deplacement vers un dossier .corbeille horodate
        cible = PROJETS / ".corbeille" / f"{nom}-{int(time.time())}"
        cible.parent.mkdir(exist_ok=True)
        shutil.move(str(p), str(cible))
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
        "polices_installees": [f.name for f in sorted((p / "analyse" / "polices").glob("*")) if f.is_file()]
        if (p / "analyse" / "polices").is_dir()
        else [],
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
    if etape not in ETAPES:
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
