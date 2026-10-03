"""Les tests du projet, lancables depuis l'interface.

    .venv/Scripts/python.exe -u test_suite.py

Le Banc mesure les MODELES. Ca ne teste pas le code. Il y a neuf fichiers
test_*.py qui verifient le schema, le registre, l'export, les agents... et
jusqu'ici ils ne se lan揣ent qu'en ligne de commande : tu ne pouvais pas les
faire tourner depuis l'interface.

Ce module les centralise : une liste declaree ici, un lanceur, et le resultat
sous forme de donnees que le serveur peut renvoyer en JSON. Sans ca, chaque
nouveau test doit etre cable deux fois — une fois ici, une fois dans l'interface
— et c'est exactement le genre de duplication qui laisse un test oublie.

Aucun test n'est appele ici : on les DECLARE. Un test declare qui n'existe pas
est signale comme tel, pas ignore silencieusement.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

RACINE = Path(__file__).resolve().parent
INTERFACE = RACINE / "interface"
PYTHON = RACINE / ".venv" / "Scripts" / "python.exe"

# La declaration unique. nom -> (fichier, ce que le test garantit)
SUITE: dict[str, tuple[str, str]] = {
    "base": ("test_base.py", "le schema SQLite et l'empreinte des PDF"),
    "registre": ("test_registre.py",
                 "presence, renommage, reassociation des PDF par empreinte"),
    "donnees": ("test_donnees.py", "glossaire, polices, calcul des orphelins"),
    "export": ("test_export.py", "l'export CSV que babeldoc sait lire"),
    "decoupe": ("test_decoupe.py", "le decoupage des pages longues"),
    "agents": ("test_agents.py",
               "les prompts et la lecture des reponses des sous-agents"),
    "banc": ("test_banc.py",
             "le Banc : catalogue, leviers, qualite, charge, reprise"),
    "serveur": ("interface/test_serveur.py",
                "les routes du serveur et leurs refus"),
    "api": ("interface/test_api.py",
            "les routes metier contre un serveur qui tourne"),
}


def decouvrir() -> tuple[list[dict], list[str]]:
    """Ce qui est declare, ce qui existe, ce qui manque.

    Un fichier test_*.py trouve mais non declare est signale : c'est un test que
    personne ne lancera jamais depuis l'interface.
    """
    out = []
    for nom, (fichier, garantie) in SUITE.items():
        chemin = RACINE / fichier
        out.append({
            "nom": nom,
            "fichier": fichier,
            "garantit": garantie,
            "existe": chemin.is_file(),
            "python": PYTHON.is_file(),
        })

    # ce fichier est le lanceur, pas un test : sinon il se signale lui-meme
    declares = {(RACINE / f).resolve() for f, _ in SUITE.values()}
    declares.add(Path(__file__).resolve())
    orphelins = [
        p.name for p in sorted(RACINE.glob("test_*.py"))
        if p.resolve() not in declares
    ]
    return out, orphelins


def lancer(noms: list[str] | None = None, timeout: int = 300) -> dict:
    """Lance les tests demandes (tous si aucun nom), et rend un rapport.

    Chaque test est lance dans son propre processus : un test qui plante ne doit
    pas empecher les autres de s'executer, et on veut son vrai code de retour.
    """
    liste, orphelins = decouvrir()
    par_nom = {t["nom"]: t for t in liste}
    choisis = [par_nom[n] for n in (noms or list(par_nom)) if n in par_nom]
    inconnus = [n for n in (noms or []) if n not in par_nom]

    resultats = []
    for t in choisis:
        debut = time.time()
        if not t["existe"]:
            resultats.append({**t, "ok": False, "code": None, "duree_s": 0,
                              "erreur": f"{t['fichier']} introuvable"})
            continue
        if not t["python"]:
            resultats.append({**t, "ok": False, "code": None, "duree_s": 0,
                              "erreur": "interpreteur .venv introuvable"})
            continue

        try:
            # on lance depuis le dossier du test : interface/test_serveur.py
            # suppose d'etre dans interface/. Depuis la racine, il echoue.
            chemin = RACINE / t["fichier"]
            # -I : mode isole. Sans lui, un PYTHONPATH herite peut faire
            # charger au .venv les paquets d'un autre environnement — c'est
            # ce qui faisait echouer test_serveur ici et pas en ligne de
            # commande.
            env = {k: v for k, v in os.environ.items()
                   if k not in ("PYTHONPATH", "PYTHONHOME")}
            p = subprocess.run(
                [str(PYTHON), "-I", "-u", chemin.name],
                cwd=str(chemin.parent), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout,
                env=env)
            sortie = (p.stdout or "") + (p.stderr or "")
            resultats.append({
                **t, "ok": p.returncode == 0, "code": p.returncode,
                "duree_s": round(time.time() - debut, 1),
                # les ECHECS sont dans la sortie : c'est ce qu'on veut voir
                "sortie": sortie.strip().split("\n")[-12:],
            })
        except subprocess.TimeoutExpired:
            resultats.append({**t, "ok": False, "code": None,
                              "duree_s": round(time.time() - debut, 1),
                              "erreur": f"delai depasse ({timeout}s)"})

    ok = sum(1 for r in resultats if r["ok"])
    return {
        "maj": datetime.now().isoformat(timespec="seconds"),
        "total": len(resultats),
        "reussis": ok,
        "tests": resultats,
        "inconnus": inconnus,
        # un test_*.py non declare ne sera jamais lance : on le signale
        "orphelins": orphelins,
        "tout_passe": ok == len(resultats) and not orphelins and not inconnus,
    }


def _auto_test() -> int:
    liste, orphelins = decouvrir()
    print(f"  {len(liste)} tests declares\n")
    for t in liste:
        marque = "ok " if t["existe"] else "MANQUANT"
        print(f"  {marque:9} {t['nom']:10} {t['garantit']}")
    if orphelins:
        print(f"\n  {len(orphelins)} test(s) non declares : {', '.join(orphelins)}")
        print("  ils existent mais personne ne les lancera depuis l'interface")
    return 0


if __name__ == "__main__":
    raise SystemExit(_auto_test())