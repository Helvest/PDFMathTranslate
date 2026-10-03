"""Controle du decoupage des pages longues pour l'extraction du glossaire.

    python test_decoupe.py

space-bunny raisonne longuement : sur une page dense, un seul appel peut
depouser le delai de l'amont. Decouper la page en blocs courts evite ça, et on
recolle les resultats.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import decoupe


def _fin(echecs: list[str]) -> int:
    if echecs:
        print("ECHECS :")
        for e in echecs:
            print(f"  - {e}")
        sys.exit(1)
    print("OK — decoupe")
    return 0


def main() -> int:
    echecs: list[str] = []

    # --- une page courte n'est pas decoupee
    if decoupe.decouper("court", 1500) != ["court"]:
        echecs.append("un texte court doit rester en un bloc")

    # --- une page longue est decoupee en ordre
    long = "\n\n".join(f"Paragraphe {i} " + "x" * 200 for i in range(20))
    blocs = decoupe.decouper(long, 1500)
    if len(blocs) < 2:
        echecs.append(f"un texte de {len(long)} car. doit etre decoupe, "
                      f"obtenu {len(blocs)} bloc(s)")
    if sum(len(b) for b in blocs) > len(long) + 40:
        echecs.append("le decoupage ajoute trop de texte")

    # rien n'est perdu
    if "".join(blocs).replace("\n\n", "") not in long.replace("\n\n", ""):
        echecs.append("le contenu est modifie par le decoupage")

    # --- on ne coupe pas au milieu d'un mot
    plat = "a" * 5000
    for b in decoupe.decouper(plat, 1000):
        if len(b) != 1000:
            echecs.append(f"un texte sans espace doit etre coupe net, obtenu {len(b)}")

    # --- les marqueurs de section sont retires
    for balise in ("CONTENT WARNING", "TRIGGER WARNING"):
        texte = f"{balise}: contains violence and horror.\n\nLe vrai contenu ici."
        net = decoupe.nettoyer(texte)
        if balise in net:
            echecs.append(f"'{balise}' non retire")
        if "Le vrai contenu" not in net:
            echecs.append(f"'{balise}' : le contenu a ete perdu")

    return _fin(echecs)


if __name__ == "__main__":
    main()