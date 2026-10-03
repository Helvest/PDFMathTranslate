"""Controle du registre des PDF dans la base.

    python test_registre.py

Le registre tient les deux promesses centrales du projet : un PDF supprime
garde ses donnees, un PDF renomme les retrouve.

Les PDF de test sont REELS (crees avec pymupdf) et pas des octets bidons :
l'identite d'un PDF est le hash de son TEXTE, donc un fichier sans texte
n'a pas d'empreinte et ne pourrait jamais etre reassocie.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import base as db
import registre


def _pdf(path: Path, texte: str) -> None:
    """Un vrai PDF, avec du texte — sans quoi il n'a pas d'empreinte."""
    import pymupdf

    with pymupdf.open() as d:
        page = d.new_page()
        page.insert_textbox(
            pymupdf.Rect(50, 50, 545, 700), texte, fontsize=11, fontname="helv"
        )
        d.save(str(path))


def _fin(echecs: list[str]) -> int:
    if echecs:
        print("ECHECS :")
        for e in echecs:
            print(f"  - {e}")
        sys.exit(1)
    print("OK — registre")
    return 0


def main() -> int:
    echecs: list[str] = []
    TEXTE = "Labyrinth of the Bride, chapter one and two."

    with tempfile.TemporaryDirectory() as tmp:
        proj = Path(tmp) / "Cloud-Empress"
        (proj / "source").mkdir(parents=True)
        (proj / "analyse").mkdir(parents=True)
        src = proj / "source"

        # --- un PDF present
        _pdf(src / "Labyrinth.pdf", TEXTE)
        with db.connecter(proj) as con:
            r = registre.synchroniser(con, proj)
            if r["presents"] != ["Labyrinth.pdf"]:
                echecs.append(f"presents : {r['presents']}")
            if r["nouveaux"] != ["Labyrinth.pdf"]:
                echecs.append(f"nouveaux : {r['nouveaux']}")
            if not db.pdf_par_nom(con, "Labyrinth.pdf")["empreinte"]:
                echecs.append("pas d'empreinte sur un vrai PDF")

        # --- idempotence : synchroniser deux fois ne change rien
        with db.connecter(proj) as con:
            registre.synchroniser(con, proj)
            n = con.execute("SELECT COUNT(*) FROM pdf").fetchone()[0]
            if n != 1:
                echecs.append(f"{n} lignes apres deux synchronisations")

        # --- le PDF disparait : l'entree reste, l'etat bascule
        (src / "Labyrinth.pdf").unlink()
        with db.connecter(proj) as con:
            r = registre.synchroniser(con, proj)
            if r["absents"] != ["Labyrinth.pdf"]:
                echecs.append(f"absents : {r['absents']}")
            if r["nouveaux"]:
                echecs.append(f"nouveaux alors que rien n'est arrive : {r['nouveaux']}")
            row = db.pdf_par_nom(con, "Labyrinth.pdf")
            if row is None:
                echecs.append("l'entree a disparu — c'est le bug qu'on evite")
            elif row["etat"] != "absent":
                echecs.append(f"etat : {row['etat']}")

        # --- le mode est preserve et filtre l'analyse
        with db.connecter(proj) as con:
            registre.definir_mode(con, "Labyrinth.pdf", "ignore")
            if registre.pdf_a_analyser(con) != []:
                echecs.append("un PDF ignore ne doit pas etre analyse")
        with db.connecter(proj) as con:
            registre.synchroniser(con, proj)
            if db.pdf_par_nom(con, "Labyrinth.pdf")["mode"] != "ignore":
                echecs.append("le mode n'a pas ete conserve a la synchronisation")

        # --- le PDF revient : present, mais toujours ignore
        _pdf(src / "Labyrinth.pdf", TEXTE)
        with db.connecter(proj) as con:
            registre.synchroniser(con, proj)
            row = db.pdf_par_nom(con, "Labyrinth.pdf")
            if row["etat"] != "present":
                echecs.append(f"au retour sur disque, etat : {row['etat']}")
            if row["mode"] != "ignore":
                echecs.append("le mode doit survivre au retour du PDF")

        # --- renommage a la main : meme contenu, autre nom -> reassociation
        (src / "Labyrinth.pdf").unlink()
        with db.connecter(proj) as con:
            registre.synchroniser(con, proj)          # marque absent
            registre.definir_mode(con, "Labyrinth.pdf", "inclus")
        _pdf(src / "Labyrinth of the Bride.pdf", TEXTE)
        with db.connecter(proj) as con:
            registre.synchroniser(con, proj)
            props = registre.reassociations(con)
            if "Labyrinth of the Bride.pdf" not in props:
                echecs.append(f"reassociations : {props}")
            else:
                ancien_id, ancien_nom = props["Labyrinth of the Bride.pdf"]
                if ancien_nom != "Labyrinth.pdf":
                    echecs.append(f"mauvais ancien : {ancien_nom}")
                registre.rattacher(con, "Labyrinth of the Bride.pdf", ancien_id)
                if db.pdf_par_nom(con, "Labyrinth.pdf") is None:
                    echecs.append("apres rattachement, l'ancien nom doit rester")
                n = con.execute("SELECT COUNT(*) FROM pdf").fetchone()[0]
                if n != 1:
                    echecs.append(f"le rattachement doit fusionner, {n} fiches restent")
                if registre.reassociations(con):
                    echecs.append("la reassociation doit disparaitre apres rattachement")

        # --- purge : on sait ce qu'on perd avant de le perdre
        with db.connecter(proj) as con:
            apercu = registre.pdf_a_purger(con, "Labyrinth of the Bride.pdf")
            if not apercu["connu"]:
                echecs.append("l'apercu doit connaitre le PDF")

    return _fin(echecs)


if __name__ == "__main__":
    main()