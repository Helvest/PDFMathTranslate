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
    "/api/banc/resultats",
]


def test_leviers_par_modele(echecs: list) -> None:
    """Chaque modele doit recevoir SON jeu de leviers, pas celui d'un autre.

    Bug reel : la detection cherchait "effort" dans le payload, or cette cle est
    presente a l'interieur meme d'un dict par modele. Un seul modele etait donc
    traite, et tous les autres recisaient ses valeurs par defaut.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import serveur
    except Exception as e:  # noqa: BLE001
        echecs.append(f"serveur non importable : {e}")
        return

    def _verifie(cond, message):
        if not cond:
            echecs.append(message)

    class _Banc:
        @staticmethod
        def effort_serieux(m):
            return "low"

    a = {"id": "modele/a", "efforts": ["low"], "raisonnement_obligatoire": True}
    b = {"id": "modele/b", "efforts": ["high"], "raisonnement_obligatoire": True}

    # le cas de l'interface : {"leviers": {id: jeu}}
    payload = {"leviers": {
        "modele/a": {"effort": "low", "max_tokens": 16000, "temperature": 0,
                     "repetitions": 2, "top_p": 0.9, "seed": 42,
                     "repetition_penalty": 1.05, "stop": "FIN",
                     "include_reasoning": False, "raisonnement": True},
        "modele/b": {"effort": "high", "max_tokens": 9000, "temperature": 0.7,
                     "repetitions": 1, "top_p": 0.5, "seed": 7,
                     "repetition_penalty": 1.2, "stop": "###",
                     "include_reasoning": True, "raisonnement": False},
    }}
    r = serveur._leviers_par_modele(payload, [a, b], _Banc)

    _verifie(set(r) == {"modele/a", "modele/b"}, f"cles attendues : {set(r)}")
    la, lb = r["modele/a"], r["modele/b"]

    _verifie(la["max_tokens"] == 16000 and lb["max_tokens"] == 9000,
             f"budgets de tokens melanges : {la['max_tokens']} / {lb['max_tokens']}")
    _verifie(la["temperature"] == 0 and lb["temperature"] == 0.7,
             f"temperatures melangees : {la['temperature']} / {lb['temperature']}")
    _verifie(lb["effort"] == "high", f"effort du modele b : {lb['effort']}")
    _verifie(lb.get("raisonnement") is False,
             "raisonnement=False doit etre transmis")
    _verifie(lb["include_reasoning"] is True, "include_reasoning=True perdu")

    # et un seul jeu, a plat : applique a tous
    plat = serveur._leviers_par_modele(
        {"effort": "low", "max_tokens": 8000, "temperature": 0.2},
        [a, b], _Banc)
    _verifie(plat["modele/a"]["max_tokens"] == plat["modele/b"]["max_tokens"] == 8000,
             "un jeu unique doit s'appliquer a tous")


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

    # les leviers sont bien distincts par modele
    test_leviers_par_modele(echecs)

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