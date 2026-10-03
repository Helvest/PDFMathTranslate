"""Un seul test : le serveur import-t-il et route-t-il encore ?

C'est le controle qui manquait. Deux commits ont supprime _projet, _fichier et
_liste_projets sans qu'on s'en apercoive, parce que mes verifications passaient
contre un ancien processus reste en ecoute sur le port.

    python test_serveur.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import serveur as s

# --- l'API doit exposer ses routes
ROUTES_ATTENDUES = [
    "/api/projets",
    "/api/etat",
    "/api/projets/{nom}",
    "/api/projets/{nom}/options",
    "/api/projets/{nom}/etape/{etape}",
    "/api/projets/{nom}/etape/suivante",
    "/api/projets/{nom}/fichier/{cle}",
    "/api/projets/{nom}/contexte/{cle}",
    "/api/polices/global",
    "/api/banc/catalogue",
        "/api/tests",
        "/api/tests/lancer",
    "/api/banc/resultats",
]


def main() -> int:
    echecs: list[str] = []

    # 1. les fonctions de securite existent et resolvent un projet
    for nom in ("_projet", "_fichier", "_liste_projets", "_etat_projet", "_corbeille"):
        if not hasattr(s, nom):
            echecs.append(f"fonction manquante : {nom}")

    # 2. chaque route annoncee existe vraiment
    declarees = {r.path for r in s.app.routes if hasattr(r, "path")}
    for r in ROUTES_ATTENDUES:
        if r not in declarees:
            echecs.append(f"route manquante : {r}")

    # 3. les fonctions utilitaires importees plus bas sont bien definies
    import re

    defs = set(re.findall(r"^def (\w+)", Path(s.__file__).read_text(encoding="utf-8"), re.M))
    # _item et _liste sont des closures locales a leur fonction
    locales = {"_item", "_liste"}
    src = Path(s.__file__).read_text(encoding="utf-8")
    for nom in set(re.findall(r"\b(_[a-z_]{3,})\(", src)) - defs - locales:
        if f"def {nom}" not in src and f"    def {nom}" not in src:
            echecs.append(f"appelee mais jamais definie : {nom}")

    # 4. la liste des projets ne leve pas
    try:
        projets = s._liste_projets()
    except Exception as e:  # noqa: BLE001
        echecs.append(f"_liste_projets a leve : {type(e).__name__}: {e}")
        projets = []

    if echecs:
        print("ECHECS :")
        for e in echecs:
            print(f"  - {e}")
        sys.exit(1)

    print(f"OK — {len(declarees)} routes, {len(projets)} projet(s)")
    for p in projets:
        print(f"  {p['nom']}  ({p['pdfs']} pdf)")
    return 0


if __name__ == "__main__":
    main()