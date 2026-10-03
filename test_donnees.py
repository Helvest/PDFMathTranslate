"""Controle des operations metier sur la base : glossaire, polices, orphelins.

    python test_donnees.py

Ce sont les requetes dont l'interface depend : compter, trier, trouver les
orphelins. Une erreur ici se voit a l'ecran, donc le test les couvre.
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
    print("OK — donnees")
    return 0


def main() -> int:
    echecs: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        proj = Path(tmp) / "Cloud-Empress"
        (proj / "analyse").mkdir(parents=True)

        with db.connecter(proj) as con:
            a = db.enregistrer_pdf(con, "A.pdf", "ea")
            b = db.enregistrer_pdf(con, "B.pdf", "eb")

            # --- glossaire et ses origines
            donnees.upsert_terme(con, "Labyrinth", "Labyrinthe", "fr", "auto")
            gid = con.execute("SELECT id FROM glossaire WHERE source=?",
                              ("Labyrinth",)).fetchone()["id"]
            donnees.lier_pages_terme(con, gid, a, [1, 2, 3])
            donnees.lier_pages_terme(con, gid, b, [1])

            # un terme de deux PDF n'est PAS orphelin si un seul survit
            if con.execute("SELECT COUNT(*) FROM page_glossaire").fetchone()[0] != 4:
                echecs.append("4 lignes page_glossaire attendues")
            orph = {r["source"] for r in donnees.termes_orphelins(con)}
            if orph:
                echecs.append(f"pas d'orphelin attendu, obtenu {orph}")

            # la suppression d'un PDF laisse le terme avec l'autre source
            donnees.termes_du_pdf(con, a)  # no-op de verification
            con.execute("DELETE FROM pdf WHERE id = ?", (a,))
            orph = {r["source"] for r in donnees.termes_orphelins(con)}
            if orph:
                echecs.append(f"B seul : le terme ne doit pas etre orphelin, {orph}")

            # --- polices
            donnees.upsert_police(con, "FuturaPT-Book", "", "defaut")
            pid = con.execute("SELECT id FROM police WHERE police_origine=?",
                              ("FuturaPT-Book",)).fetchone()["id"]
            donnees.lier_police(con, pid, b, 148, 3)
            pol = donnees.lister_polices(con)[0]
            if pol["spans"] != 148 or pol["pages"] != 3:
                echecs.append(f"usage de police : {pol['spans']}/{pol['pages']}")

            # --- orphelins quand tout a disparu
            con.execute("DELETE FROM pdf WHERE id = ?", (b,))
            orph_t = {r["source"] for r in donnees.termes_orphelins(con)}
            orph_p = {r["police_origine"] for r in donnees.polices_orphelines(con)}
            if orph_t != {"Labyrinth"}:
                echecs.append(f"termes orphelins : {orph_t}")
            if orph_p != {"FuturaPT-Book"}:
                echecs.append(f"polices orphelines : {orph_p}")

            # le compteur d'orphelins doit correspondre
            n = donnees.compter(con)
            if n["termes_orphelins"] != 1 or n["polices_orphelines"] != 1:
                echecs.append(f"compteur : {n}")

            # --- purge des orphelins
            donnees.purger_termes_orphelins(con)
            donnees.purger_polices_orphelines(con)
            if con.execute("SELECT COUNT(*) FROM glossaire").fetchone()[0] != 0:
                echecs.append("la purge des termes orphelins a echoue")
            if con.execute("SELECT COUNT(*) FROM police").fetchone()[0] != 0:
                echecs.append("la purge des polices orphelines a echoue")

        # --- une base sans donnees ne doit rien lever
        with tempfile.TemporaryDirectory() as tmp2:
            vide = Path(tmp2) / "Vide"
            (vide / "analyse").mkdir(parents=True)
            with db.connecter(vide) as con:
                if donnees.lister_termes(con) != []:
                    echecs.append("liste vide attendue")
                if donnees.compter(con)["termes"] != 0:
                    echecs.append("compteur vide attendu")

    return _fin(echecs)


if __name__ == "__main__":
    main()