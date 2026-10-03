"""Controle de l'export du glossaire pour BabelDOC.

    python test_export.py

Le CSV est le SEUL pont vers BabelDOC : babeldoc/glossary.py ne lit que
source, target, target_language. Cet export est donc temporaire par nature —
le test verifie qu'il est exact, et qu'il disparait apres usage.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import base as db
import donnees


def _fin(echecs: list[str]) -> int:
    if echecs:
        print("ECHECS :")
        for e in echecs:
            print(f"  - {e}")
        sys.exit(1)
    print("OK — export")
    return 0


def main() -> int:
    echecs: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        proj = Path(tmp) / "Cloud-Empress"
        (proj / "analyse").mkdir(parents=True)

        with db.connecter(proj) as con:
            pid = db.enregistrer_pdf(con, "A.pdf", "ea")
            gid = donnees.upsert_terme(con, "Labyrinth", "Labyrinthe")
            donnees.lier_pages_terme(con, gid, pid, [1])
            # un terme source == cible est normal (nom propre) : il doit
            # figurer dans l'export, sinon la traduction n'est pas contrainte
            donnees.upsert_terme(con, "Imago", "Imago")
            # un terme sans cible ne sert a rien : il ne doit PAS etre exporte
            donnees.upsert_terme(con, "Incomplet", "")

            csv_p = donnees.exporter_glossaire_csv(con, proj / "glossaire.csv")
            texte = csv_p.read_text(encoding="utf-8-sig")

            lignes = [l for l in texte.splitlines() if l.strip()]
            if lignes[0] != "source,target,tgt_lng":
                echecs.append(f"en-tete : {lignes[0]}")
            corps = set(lignes[1:])
            if ("Labyrinth,Labyrinthe,fr") not in corps:
                echecs.append(f"terme absent : {corps}")
            if ("Imago,Imago,fr") not in corps:
                echecs.append("un nom propre (source==cible) doit etre exporte")
            if any(l.startswith("Incomplet") for l in lignes[1:]):
                echecs.append("un terme sans cible ne doit pas etre exporte")

            # tri : alphabetique, insensible a la casse — l'interface trie
            # aussi, et un export trie est plus lisible si on l'ouvre
            ordre = [l.split(",")[0] for l in lignes[1:]]
            if ordre != sorted(ordre, key=str.lower):
                echecs.append(f"pas trie : {ordre}")

            # le fichier cree EST celui qu'on passe a babeldoc
            if not csv_p.is_file():
                echecs.append("le fichier n'a pas ete cree")

    # le fichier temporaire est-il nettoye ?
    if csv_p.is_file():
        echecs.append("l'export doit etre supprime apres usage")

    return _fin(echecs)


if __name__ == "__main__":
    main()