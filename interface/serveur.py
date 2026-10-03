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
# Donnees globales, hors projet : resultats de bench, textes de reference.
GLOBAL = RACINE.parent / "Global"
BENCH_DIR = GLOBAL / "bench"
BENCH_RESULTATS = BENCH_DIR / "resultats.json"
BENCH_SCRIPT = DEPOT / "bench.py"
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
    # space-bunny-alpha repond toujours et supporte json_object (verifie).
    # Les modeles gratuits epuisent leur quota (429) ; celui-ci ne l'a pas.
    "model": "stealth/space-bunny-alpha",
    "term_model": "stealth/space-bunny-alpha",
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
# Processus en cours, par (projet, etape) : pour pouvoir les arreter.
PROCESSUS: dict[tuple[str, str], subprocess.Popen] = {}
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

# Etat du bench : global, un seul a la fois.
BENCH: dict = {"etat": "pret", "log": "", "debut": None, "erreur": ""}


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
        # .corbeille et .downloads sont des dossiers techniques, pas des projets
        if not d.is_dir() or d.name.startswith("."):
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
        # une pause posee a la main n'est pas ecrasee par le code de retour :
        # le processus est tue, donc son code n'est pas un echec reel
        if e.get("etat") == "pause":
            PROCESSUS.pop((projet, etape), None)
            return
        e["etat"] = "termine" if proc.returncode == 0 else "erreur"
        e["fin"] = datetime.now().isoformat(timespec="seconds")
        if proc.returncode != 0:
            e["erreur"] = f"code {proc.returncode}"
    PROCESSUS.pop((projet, etape), None)


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
            "PDF2ZH_GLOSSAIRE_AUTO": "0" if o.get("glossaire_manuel", True) else "1",
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
    PROCESSUS[(projet, etape)] = proc
    threading.Thread(target=_suivre, args=(projet, etape, proc), daemon=True).start()


def _corbeille(chemin: Path) -> dict:
    """Envoie un fichier ou un dossier a la corbeille. Jamais de suppression.

    Deux mecanismes : la corbeille Windows (send2trash), puis un repli dans
    Projets/.corbeille/. On bascule sur le repli des que send2trash echoue,
    pour n'importe quelle raison — module absent, permission, volume non
    supporte. Sans ce repli, un echec laissait le fichier sur place sans rien
    dire.

    Retourne {"methode": ..., "destination": ...} pour l'afficher.
    """
    try:
        from send2trash import send2trash  # type: ignore

        send2trash(str(chemin))
        return {"methode": "corbeille Windows", "destination": ""}
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001 - on bascule toujours sur le repli
        print(f"send2trash a echoue ({type(e).__name__}: {e}) -> repli local")

    horodatage = datetime.now().strftime("%Y%m%d-%H%M%S")
    repli = PROJETS / ".corbeille" / f"{horodatage}-{chemin.name}"
    repli.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(chemin), str(repli))
    return {"methode": "repli local", "destination": str(repli)}


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
    """Toutes les polices disponibles, groupees par provenance.

    Trois sources : le projet, le global partage, et les polices embarquees
    par BabelDOC. Le tout forme la liste deroulante des remplacements.
    """
    import catalogue_polices as catalogue

    try:
        cat = catalogue.charger(p)
    except Exception as e:  # noqa: BLE001 - l'interface ne doit pas tomber
        return {
            "projet": [], "global": [], "babeldoc": [], "toutes": [],
            "installees": [], "telechargees": [], "proposees": [],
            "erreur": f"{type(e).__name__}: {e}",
        }

    def _item(pol: "catalogue.Police") -> dict:
        return {"nom": pol.nom, "famille": pol.famille, "style": pol.style}

    # polices presentes dans les archives deballees de downloads/
    telechargees = []
    dd = p / "downloads"
    if dd.is_dir():
        for f in sorted(dd.rglob("*")):
            if f.is_file() and f.suffix.lower() in EXT_POLICE:
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

    du_projet = [_item(x) for x in cat.par_source("projet")]
    globales = [_item(x) for x in cat.par_source("global")]
    de_babeldoc = [_item(x) for x in cat.par_source("babeldoc")]

    return {
        "projet": du_projet,
        "global": globales,
        "babeldoc": de_babeldoc,
        "toutes": [x["nom"] for x in du_projet + globales + de_babeldoc],
        # conserves pour l'affichage des archives
        "installees": [x["nom"] for x in du_projet],
        "telechargees": telechargees,
        "proposees": proposees,
        "mapping": {
            nom: {"fichier": f, "origine": o, "source": s}
            for nom, (f, o, s) in cat.mapping.items()
        },
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


@app.post("/api/projets/{nom}/police/installer")
def api_installer_police(nom: str, payload: dict):
    """Installe une police dans le projet : copie le fichier, remplit le CSV.

    'Installer' = copier le .ttf dans analyse/polices/ du projet. Le fichier
    peut venir de Global/polices/, du cache de BabelDOC, ou de downloads/.
    La ligne du CSV est remplie automatiquement (origine=auto).
    """
    import csv as _csv

    import catalogue_polices as catalogue

    p = _projet(nom)
    if not p.is_dir():
        raise HTTPException(404, "projet introuvable")

    fichier = (payload.get("fichier") or "").strip()
    origine_nom = (payload.get("police_origine") or "").strip()
    if not fichier:
        raise HTTPException(400, "fichier requis")

    # 1. trouver la source
    src_path, source = catalogue.resoudre(fichier, p)
    if src_path is None:
        # peut-etre dans downloads/ du projet
        dd = p / "downloads"
        cand = [f for f in dd.rglob(fichier) if f.is_file()] if dd.is_dir() else []
        if cand:
            src_path, source = cand[0], "downloads"
    if src_path is None:
        raise HTTPException(404, f"police introuvable : {fichier}")

    # 2. copier dans le projet
    dest_dir = p / "analyse" / "polices"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src_path.name
    if not dest.exists() or dest.read_bytes() != src_path.read_bytes():
        shutil.copy2(src_path, dest)

    # 3. remplir la ligne du CSV si une police d'origine est donnee
    if origine_nom:
        csv_path = p / "analyse" / "polices.csv"
        entete = ["police_origine", "remplacement", "origine", "propose",
                  "raison", "famille", "spans", "pages", "pdfs"]
        lignes: list[dict] = []
        if csv_path.is_file():
            with csv_path.open(encoding="utf-8-sig", newline="") as f:
                lignes = list(_csv.DictReader(f))

        trouve = False
        for l in lignes:
            if (l.get("police_origine") or "").strip() == origine_nom:
                l["remplacement"] = dest.name
                l["origine"] = "auto"
                trouve = True
                break
        if not trouve:
            lignes.append({
                "police_origine": origine_nom, "remplacement": dest.name,
                "origine": "auto", "propose": "", "raison": "",
                "famille": "", "spans": "", "pages": "", "pdfs": "",
            })

        csv_path.parent.mkdir(parents=True, exist_ok=True)
        with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=entete)
            w.writeheader()
            for l in lignes:
                w.writerow({k: l.get(k, "") for k in entete})

    return {
        "ok": True,
        "installe": dest.name,
        "depuis": source,
        "chemin": str(dest),
    }


@app.get("/api/polices/global")
def api_polices_global():
    """Le catalogue global, toutes sources confondues (hors projet)."""
    import catalogue_polices as catalogue

    cat = catalogue.charger(None)
    return {
        "global": [
            {"nom": x.nom, "famille": x.famille, "style": x.style}
            for x in cat.par_source("global")
        ],
        "babeldoc": [
            {"nom": x.nom, "famille": x.famille, "style": x.style}
            for x in cat.par_source("babeldoc")
        ],
        "dossier": str(catalogue.POLICES_GLOBALES),
        "csv": str(catalogue.CSV_GLOBAL),
        "mapping": {n: {"fichier": f, "origine": o} for n, (f, o, _s) in cat.mapping.items()},
    }


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


# ---------------------------------------------------------------- api metier
#
# Ces routes lisent et ecrivent etat.db. Tout passe par la base : plus de
# CSV, plus de JSON a synchroniser.

def _base(nom: str):
    """(projet, connexion). Erreur claire si la base est absente ou cassee."""
    import base as _b

    p = _projet(nom)
    if not p.is_dir():
        raise HTTPException(404, "projet introuvable")
    try:
        return p, _b.connecter(p)
    except _b.BaseInvalide as e:
        raise HTTPException(500, str(e)) from e


@app.get("/api/projets/{nom}/pdfs")
def api_pdfs(nom: str):
    """Tous les PDF connus : presents ET absents.

    Un PDF absent garde son entree et ses donnees ; c'est l'utilisateur qui
    decide de l'ignorer ou de le purger.
    """
    import registre

    p, ctx = _base(nom)
    with ctx as con:
        registre.synchroniser(con, p)
        presents = (
            sorted(x.name for x in (p / "source").glob("*.pdf"))
            if (p / "source").is_dir() else []
        )
        out = []
        for r in con.execute("SELECT * FROM pdf ORDER BY nom"):
            out.append({
                "id": r["id"],
                "nom": r["nom"],
                "etat": r["etat"],
                "mode": r["mode"],
                "empreinte": (r["empreinte"] or "")[:12],
                "anciens": json.loads(r["ancien_noms"] or "[]"),
                "present": r["nom"] in presents,
                "recalculer": None if r["recalculer"] is None else bool(r["recalculer"]),
                "contexte": con.execute(
                    "SELECT 1 FROM contexte WHERE pdf_id = ?", (r["id"],)
                ).fetchone() is not None,
            })
        return {
            "pdfs": out,
            "reassociations": {
                nom_actuel: ancien
                for nom_actuel, (_id, ancien) in registre.reassociations(con).items()
            },
            "sur_disque": presents,
        }


@app.get("/api/projets/{nom}/compteurs")
def api_compteurs(nom: str):
    """Les compteurs pour la banniere et les pastilles des onglets."""
    import donnees

    _p, ctx = _base(nom)
    with ctx as con:
        return donnees.compter(con)


@app.get("/api/projets/{nom}/glossaire")
def api_glossaire(nom: str, orphelins: int = 0):
    """Tous les termes, avec leurs sources et leurs pages.

    ?orphelins=1 ne renvoie que les termes dont plus aucun PDF source n'existe.
    """
    import donnees

    _p, ctx = _base(nom)
    with ctx as con:
        return donnees.lister_termes(con, orphelins_seulement=bool(orphelins))


@app.put("/api/projets/{nom}/glossaire")
def api_ecrire_glossaire(nom: str, payload: dict):
    """Enregistre les termes modifies depuis l'interface.

    Les cibles editees a la main passent en origine='manuel' : elles ne
    seront jamais ecrasees par une re-analyse.
    """
    import donnees

    _p, ctx = _base(nom)
    with ctx as con:
        n = 0
        for t in payload.get("termes") or []:
            source = (t.get("source") or "").strip()
            cible = (t.get("target") or "").strip()
            if not source:
                continue
            donnees.upsert_terme(con, source, cible, t.get("tgt_lng") or "fr", "manuel")
            n += 1
        return {"ok": True, "enregistres": n}


@app.delete("/api/projets/{nom}/glossaire/{terme_id}")
def api_supprimer_terme(nom: str, terme_id: int):
    """Supprime un terme. Definitif : l'interface confirme avant."""
    _p, ctx = _base(nom)
    with ctx as con:
        cur = con.execute("DELETE FROM glossaire WHERE id = ?", (terme_id,))
        if not cur.rowcount:
            raise HTTPException(404, "terme introuvable")
        return {"ok": True}


@app.post("/api/projets/{nom}/glossaire/purger-orphelins")
def api_purger_orphelins(nom: str):
    """Efface les termes dont plus aucun PDF source n'existe.

   .appelee apres une confirmation qui detaillant le nombre.
    """
    import donnees

    _p, ctx = _base(nom)
    with ctx as con:
        n = donnees.purger_termes_orphelins(con)
        return {"ok": True, "supprimes": n}


@app.get("/api/projets/{nom}/polices")
def api_polices(nom: str, orphelines: int = 0):
    """Toutes les polices, avec leur usage par PDF."""
    import donnees

    _p, ctx = _base(nom)
    with ctx as con:
        return donnees.lister_polices(con, orphelines_seulement=bool(orphelines))


@app.put("/api/projets/{nom}/polices")
def api_ecrire_polices(nom: str, payload: dict):
    """Enregistre les remplacements choisis dans l'interface."""
    import donnees

    _p, ctx = _base(nom)
    with ctx as con:
        n = 0
        for x in payload.get("polices") or []:
            origine = (x.get("police_origine") or "").strip()
            if not origine:
                continue
            donnees.upsert_police(
                con, origine, (x.get("remplacement") or "").strip(),
                x.get("origine") or "manuel", x.get("propose") or "",
                x.get("raison") or "", x.get("famille") or "",
            )
            n += 1
        return {"ok": True, "enregistres": n}


@app.post("/api/projets/{nom}/polices/purger-orphelines")
def api_purger_polices_orphelines(nom: str):
    import donnees

    _p, ctx = _base(nom)
    with ctx as con:
        n = donnees.purger_polices_orphelines(con)
        return {"ok": True, "supprimes": n}


@app.post("/api/projets/{nom}/pdf/mode")
def api_mode_pdf(nom: str, payload: dict):
    """Change le mode d'un PDF : inclus ou ignore.

    Un PDF ignore reste dans source/ et garde ses donnees ; il est seulement
    exclu des prochaines analyses.
    """
    import registre

    _p, ctx = _base(nom)
    with ctx as con:
        nom_pdf = (payload.get("nom") or "").strip()
        mode = (payload.get("mode") or "").strip()
        if mode not in {"inclus", "ignore"}:
            raise HTTPException(400, "mode doit etre 'inclus' ou 'ignore'")
        if not any(r["nom"] == nom_pdf for r in con.execute("SELECT nom FROM pdf")):
            raise HTTPException(404, "PDF inconnu du registre")
        registre.definir_mode(con, nom_pdf, mode)
        return {"ok": True, "nom": nom_pdf, "mode": mode}


@app.post("/api/projets/{nom}/pdf/renommer")
def api_renommer_pdf(nom: str, payload: dict):
    """Renomme un PDF et son entree de registre.

    Tout ce qui etait rattache suit : les tables referencent des id.
    """
    import base as _b
    import registre

    p, ctx = _base(nom)
    ancien = (payload.get("ancien") or "").strip()
    nouveau = (payload.get("nouveau") or "").strip()
    if not ancien or not nouveau:
        raise HTTPException(400, "ancien et nouveau sont requis")
    if not re.fullmatch(r".+\.pdf", nouveau, re.IGNORECASE):
        raise HTTPException(400, "le nouveau nom doit finir par .pdf")

    src = p / "source" / ancien
    if src.is_file():
        src.rename(p / "source" / nouveau)

    with ctx as con:
        row = _b.pdf_par_nom(con, ancien)
        if row is None:
            raise HTTPException(404, "PDF inconnu du registre")
        try:
            _b.renommer_pdf(con, int(row["id"]), nouveau)
        except _b.Doublon as e:
            raise HTTPException(409, f"le nom '{nouveau}' est deja pris") from e
    return {"ok": True, "nom": nouveau}


@app.post("/api/projets/{nom}/pdf/rattacher")
def api_rattacher_pdf(nom: str, payload: dict):
    """Le PDF 'nouveau' reprend l'identite de l'ancien : ses donnees suivent."""
    import registre

    _p, ctx = _base(nom)
    with ctx as con:
        nouveau = (payload.get("nouveau") or "").strip()
        ancien_id = payload.get("ancien_id")
        try:
            registre.rattacher(con, nouveau, int(ancien_id))
        except KeyError as e:
            raise HTTPException(404, f"PDF introuvable : {e}") from e
        return {"ok": True}


@app.get("/api/projets/{nom}/pdf/{fichier}/apercu-purge")
def api_apercu_purge(nom: str, fichier: str):
    """Ce que la suppression de ce PDF ferait perdre. Affiche AVANT.

    La suppression est definitive : mieux vaut voir.
    """
    import registre

    _p, ctx = _base(nom)
    with ctx as con:
        return registre.pdf_a_purger(con, fichier)


@app.post("/api/projets/{nom}/pdf/supprimer-donnees")
def api_supprimer_donnees(nom: str, payload: dict):
    """Supprime le CONTEXTE de ce PDF, et uniquement lui.

    Ses termes de glossaire et ses polices ne sont pas touches : ils peuvent
    venir d'autres PDF, et deviendront orphelins si besoin.
    """
    import registre

    _p, ctx = _base(nom)
    with ctx as con:
        try:
            return registre.supprimer_donnees(con, (payload.get("nom") or "").strip())
        except KeyError as e:
            raise HTTPException(404, f"PDF introuvable : {e}") from e


@app.post("/api/projets/{nom}/pdf/supprimer-entree")
def api_supprimer_entree(nom: str, payload: dict):
    """Supprime la FICHE du PDF. Ses donnees deviennent orphelines.

    Definitif : l'interface a montre l'apercu avant.
    """
    import registre

    _p, ctx = _base(nom)
    with ctx as con:
        try:
            return registre.supprimer_entree(con, (payload.get("nom") or "").strip())
        except KeyError as e:
            raise HTTPException(404, f"PDF introuvable : {e}") from e


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


@app.get("/api/projets/{nom}/etape/suivante")
def api_etape_suivante(nom: str):
    """Quelle etape lancer ensuite, et ce qui manque avant de la lancer.

    Le workflow est concu pour une pause entre chaque etape : on valide a la
    main entre les etapes, donc rien n'est enchaine automatiquement.
    """
    p = _projet(nom)
    if not p.is_dir():
        raise HTTPException(404, "projet introuvable")

    # --- ou en est chaque etape ?
    etats = _etat_projet(nom)

    # --- l'ordre du workflow
    ordre = [
        ("contexte", "Contexte", lambda: (p / "analyse" / "contexte").is_dir()
            and any(f.glob("*.json") for f in [p / "analyse" / "contexte"])),
        ("polices", "Polices", lambda: (p / "analyse" / "polices.csv").is_file()),
        ("glossaire", "Glossaire", lambda: (p / "analyse" / "glossaire.csv").is_file()),
        ("traduire", "Traduire", lambda: any((p / "traduits").glob("*.pdf"))),
    ]

    # Sans PDF, aucune étape n'a de sens : proposer "Contexte" sur un projet
    # vide ne ferait qu'échouer aussitôt.
    srcs = sorted(x.name for x in (p / "source").glob("*.pdf")) if (p / "source").is_dir() else []
    deja_traduits = (
        bool(list((p / "traduits").glob("*.pdf"))) if (p / "traduits").is_dir() else False
    )

    # la première étape non faite
    suivante = None
    if srcs or deja_traduits:
        for cle, libelle, fait in ordre:
            if not fait():
                suivante = (cle, libelle)
                break

    # --- ce qui manque, et ce qui est deja fait
    analyse = p / "analyse"
    o = _options(nom)

    # glossaire : compte les termes et les pieges source == cible
    n_termes = 0
    n_pieges = 0
    g = analyse / "glossaire.csv"
    if g.is_file():
        import csv as _csv

        try:
            with g.open(encoding="utf-8-sig", newline="") as f:
                for l in _csv.DictReader(f):
                    if not l.get("source"):
                        continue
                    n_termes += 1
                    if (l["source"] or "").strip().lower() == (l.get("target") or "").strip().lower():
                        n_pieges += 1
        except (OSError, csv.Error):
            pass

    # polices : comptees et choisies
    n_polices = n_choisies = 0
    pc = analyse / "polices.csv"
    if pc.is_file():
        import csv as _csv

        try:
            with pc.open(encoding="utf-8-sig", newline="") as f:
                for l in _csv.DictReader(f):
                    if not l.get("police_origine"):
                        continue
                    n_polices += 1
                    if (l.get("remplacement") or "").strip():
                        n_choisies += 1
        except (OSError, csv.Error):
            pass

    # contexte : un json par PDF
    n_contextes = 0
    ctx = analyse / "contexte"
    if ctx.is_dir():
        n_contextes = len([f for f in ctx.glob("*.json") if not f.name.startswith("_")])

    if not srcs:
        raison = "Depose des PDF dans source/ pour commencer"
    elif not suivante:
        raison = "Toutes les etapes sont faites"
    else:
        raison = ""

    return {
        "suivante": suivante[0] if suivante else None,
        "libelle": suivante[1] if suivante else "",
        "raison": raison,
        "etats": etats,
        "etat_global": (
                "vide" if not srcs else ("termine" if not suivante else "en_cours")
            ),
        "resume": {
            "pdfs_source": len(srcs),
            "pdfs_selectionnes": len(o.get("pdfs_analyser") or []),
            "pdfs_traduire": len(o.get("pdfs_traduire") or []),
            "traduits": len(list((p / "traduits").glob("*.pdf"))) if (p / "traduits").is_dir() else 0,
            "contextes": n_contextes,
            "polices": n_polices,
            "polices_choisies": n_choisies,
            "termes": n_termes,
            "termes_pieges": n_pieges,
        },
    }


@app.post("/api/projets/{nom}/etape/pause")
def api_pause(nom: str):
    """Arrete la tache en cours.

    Pas besoin d'ecrire d'etat : relancer suffit. Le cache BabelDOC evite de
    retraduire les paragraphes deja faits, donc rien n'est perdu.
    """
    _projet(nom)
    with VERROU:
        en_cours = [
            e for e, v in ETATS.get(nom, {}).items() if v.get("etat") == "en_cours"
        ]

    if not en_cours:
        return {"ok": True, "rien": True}

    arretes = []
    for etape in en_cours:
        proc = PROCESSUS.get((nom, etape))
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
            arretes.append(etape)
        with VERROU:
            e = ETATS.get(nom, {}).get(etape)
            if e is not None:
                e["etat"] = "pause"
                e["erreur"] = "arretee — relance pour reprendre"

    return {"ok": True, "arretes": arretes, "reprendable": True}


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


# ---------------------------------------------------------------- bench


@app.get("/api/bench/modeles")
def api_bench_modeles():
    """Les modeles gratuits proposes par le proxy, pour cocher ceux a tester."""
    import urllib.request

    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8645/v1/models",
            headers={"Authorization": "Bearer hermes"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.load(r)
    except Exception as e:
        return {"modeles": [], "erreur": f"proxy injoignable ({type(e).__name__})"}

    # Tous les modeles, pas seulement les ":free" : space-bunny-alpha n'a pas
    # de suffixe gratuit et repond meme quand les quotas gratuits sont epuises.
    noms = sorted(m.get("id", "") for m in d.get("data", []))
    gratuits = [n for n in noms if n.endswith(":free")]
    return {
        "modeles": noms,
        "gratuits": gratuits,
        "erreur": "",
    }


@app.get("/api/bench/resultats")
def api_bench_resultats():
    """Resultats du dernier bench + l'etat du run en cours."""
    if BENCH_RESULTATS.is_file():
        try:
            d = json.loads(BENCH_RESULTATS.read_text(encoding="utf-8"))
            d["etat"] = BENCH["etat"]
            d["log"] = BENCH["log"][-8000:]
            d["erreur_run"] = BENCH.get("erreur", "")
            return d
        except (json.JSONDecodeError, OSError) as e:
            return {"configs": {}, "etat": BENCH["etat"], "erreur": str(e)}
    return {
        "configs": {},
        "maj": None,
        "etat": BENCH["etat"],
        "log": BENCH["log"][-8000:],
        "erreur_run": BENCH.get("erreur", ""),
    }


@app.post("/api/bench/lancer")
def api_bench_lancer(payload: dict):
    """Lance un bench. modeles et configs sont des listes."""
    if BENCH["etat"] == "en_cours":
        raise HTTPException(409, "un test tourne deja")

    modeles = payload.get("modeles") or []
    configs = payload.get("configs") or ["rapide"]
    workers = int(payload.get("workers") or 3)

    cmd = [str(PY), str(BENCH_SCRIPT), "--workers", str(workers)]
    if modeles:
        cmd += ["--modeles", ",".join(modeles)]
    cmd += ["--configs", ",".join(configs)]

    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    BENCH.update(
        etat="en_cours",
        log="",
        debut=datetime.now().isoformat(timespec="seconds"),
        erreur="",
    )

    proc = subprocess.Popen(
        cmd,
        cwd=str(DEPOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
        bufsize=1,
    )
    threading.Thread(target=_suivre_bench, args=(proc,), daemon=True).start()
    return {"ok": True, "appels": len(modeles) or "tous"}


def _suivre_bench(proc: subprocess.Popen) -> None:
    """Suit la sortie du bench et alimente BENCH."""
    lignes: list[str] = []
    for ligne in proc.stdout:  # type: ignore[union-attr]
        lignes.append(ligne.rstrip("\n"))
        if len(lignes) > 500:
            del lignes[:150]
        BENCH["log"] = "\n".join(lignes)
    proc.wait()
    BENCH["etat"] = "termine" if proc.returncode == 0 else "erreur"
    if proc.returncode != 0:
        BENCH["erreur"] = f"code {proc.returncode}"


@app.post("/api/bench/arreter")
def api_bench_arreter():
    """Arrete un bench en cours."""
    import subprocess as sp

    if BENCH["etat"] != "en_cours":
        return {"ok": True, "rien": True}
    # on tue les bench.py en cours
    try:
        sp.run(
            ["taskkill", "/F", "/IM", "python.exe", "/FI", "WINDOWTITLE eq bench*"],
            capture_output=True,
        )
    except Exception:
        pass
    BENCH["etat"] = "arrete"
    return {"ok": True}


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
