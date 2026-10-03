"""Controle des sous-agents d'extraction et de la fusion.

    python test_agents.py

L'extraction se fait en parallele, sans contexte partage ; la fusion se fait
ensuite, avec tout. Ce test verifie les deux moities, sans appel reseau.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agents


def _fin(echecs: list[str]) -> int:
    if echecs:
        print("ECHECS :")
        for e in echecs:
            print(f"  - {e}")
        sys.exit(1)
    print("OK — agents")
    return 0


def main() -> int:
    echecs: list[str] = []

    # ================= le prompt d'extraction =================
    base = "Terre Brûlée. La Longue Marche commence."

    tout = {"ctx_consignes": True, "ctx_lot": True,
            "ctx_document": True, "ctx_page": True}
    rien = {k: False for k in tout}

    # par defaut, tout est coche : consignes + contextes
    p = agents.prompt_extraction(
        base, consignes="CE=ce", lot="LOT", document="DOC", page="PAGE")
    for attendu in ("CONSIGNES", "CONTEXTE DU LOT", "DÉFINITION"):
        if attendu not in p:
            echecs.append(f"prompt complet sans '{attendu}'")
    if "Terre Brûlée" not in p:
        echecs.append("le texte n'est pas dans le prompt")

    # tout decoche -> prompt minimal, juste le texte
    p2 = agents.prompt_extraction(
        base, consignes="CE=ce", lot="LOT", document="DOC", page="PAGE", ctx=rien)
    if "CONSIGNES" in p2 or "CONTEXTE" in p2:
        echecs.append("un prompt sans contexte ne doit contenir ni consignes ni contexte")
    if "Terre Brûlée" not in p2:
        echecs.append("le texte doit toujours etre present")

    # selection partielle : consignes seules
    p3 = agents.prompt_extraction(
        base, consignes="CE=ce", lot="LOT",
        ctx={**rien, "ctx_consignes": True})
    if "CONSIGNES" not in p3:
        echecs.append("consignes seules : le bloc CONSIGNES manque")
    if "CONTEXTE DU LOT" in p3:
        echecs.append("consignes seules : le contexte lot ne doit pas etre la")

    # ================= le prompt de fusion =================
    doublons = [
        {"src": "Bakto", "tgt": "Bakto", "definition": "Nom propre, ne se traduit pas"},
        {"src": "Bakto", "tgt": "Bakto", "definition": "Personnage principal"},
        {"src": "Bakto", "tgt": "Backto", "definition": "Autre agent a corrige a tort"},
        {"src": "Farmerling", "tgt": "Farmerling", "definition": "Mot invente, garde tel quel"},
    ]
    f = agents.prompt_fusion(doublons)
    for attendu in ("Bakto", "Farmerling", "CONSISTENCE"):
        if attendu not in f:
            echecs.append(f"prompt de fusion sans '{attendu}'")

    # ================= les options de selection =================
    d = agents.DEFAUTS
    for cle in ("ctx_consignes", "ctx_lot", "ctx_document", "ctx_page"):
        if cle not in d:
            echecs.append(f"option manquante : {cle}")
        elif d[cle] is not True:
            echecs.append(f"{cle} devrait etre cochee par defaut")

    # ================= le decoupage en taches =================
    # une page courte = une tache ; une page longue = plusieurs blocs
    pages = [
        ("A.pdf", 1, "texte court"),
        ("A.pdf", 2, "autre page courte"),
        ("B.pdf", 1, "encore court"),
    ]
    taches = agents.planifier(pages)
    if len(taches) != 3:
        echecs.append(f"3 pages courtes -> 3 taches, obtenu {len(taches)}")
    for t in taches:
        if set(t) != {"pdf", "page", "texte"}:
            echecs.append(f"tache mal formee : {t}")

    # 3000 caracteres depassent BLOC_MAX (1500) : la page est decoupee
    if len(agents.planifier([("A.pdf", 9, "x" * 3000)])) < 2:
        echecs.append("une page de 3000 car. doit etre decoupee en blocs")

    # une page d'une seule ligne, sans espaces, doit aussi etre decoupee
    if len(agents.planifier([("A.pdf", 9, "y" * 8000)])) < 2:
        echecs.append("une page de 8000 car. sans espaces doit etre decoupee")

    # une page vide ne donne aucune tache
    if agents.planifier([("A.pdf", 1, "   ")]) != []:
        echecs.append("une page vide ne doit donner aucune tache")

    # ================= la reponse d'un agent =================
    # du JSON avec des accents, des echappements, du bruit autour
    brut = '''Voici les termes :
[{"src": "Lowland Wastes", "tgt": "Terres Basses", "definition": "Region desertique du jeu."},
 {"src": "B\\"eau", "tgt": "Eau", "definition": "Avec un guillemet."}]
J'espere que cela aide.'''
    r = agents.lire_reponse(brut)
    if len(r) != 2:
        echecs.append(f"2 termes attendus, obtenu {len(r)}")
    if r and r[0]["src"] != "Lowland Wastes":
        echecs.append(f"src mal lu : {r[0]['src']}")
    if r and not r[0].get("definition"):
        echecs.append("la definition est vide")

    # une reponse parasite ne doit pas faire echouer l'agent
    if agents.lire_reponse("pas du JSON du tout") != []:
        echecs.append("une reponse sans JSON doit rendre une liste vide")
    if agents.lire_reponse("") != []:
        echecs.append("une reponse vide doit rendre une liste vide")

    # un terme sans traduction n'est pas utilisable
    r2 = agents.lire_reponse('[{"src":"X","tgt":"","definition":"d"}]')
    if len(r2) != 0:
        echecs.append("un terme sans traduction doit etre ecarte")

    return _fin(echecs)


if __name__ == "__main__":
    main()