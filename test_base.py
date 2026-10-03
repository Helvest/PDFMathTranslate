"""Controle du schema et de l'acces a la base.

    python test_base.py

Sort 0 si tout va bien, 1 sinon. Le schema est verifie table par table, et
l'aller-retour ecrit/lu sur les PDF est verifie aussi : c'est la base de tout
le reste.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import base as db

TABLES = {
    "pdf", "pdf_noms", "contexte", "page_contexte", "lot",
    "glossaire", "page_glossaire", "police", "police_polices",
}


def _fin(echecs: list[str]) -> int:
    if echecs:
        print("ECHECS :")
        for e in echecs:
            print(f"  - {e}")
        sys.exit(1)
    print("OK — schema")
    return 0


def main() -> int:
    echecs: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        proj = Path(tmp) / "Cloud-Empress"
        (proj / "analyse").mkdir(parents=True)

        # --- la base se cree toute seule au premier appel
        with db.connecter(proj):
            pass
        f = proj / "analyse" / "etat.db"
        if not f.is_file():
            echecs.append("etat.db absent apres connecter")
            return _fin(echecs)

        with db.connecter(proj) as con:
            trouvees = {
                r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")
            }
        manquantes = TABLES - trouvees
        if manquantes:
            echecs.append(f"tables manquantes : {sorted(manquantes)}")

        # --- aller-retour sur un PDF
        with db.connecter(proj) as con:
            pid = db.enregistrer_pdf(con, "Labyrinth.pdf", "abc123")
            db.mettre_pdf_etat(con, pid, "absent")
            e = db.lire_pdf(con, pid)
            if e is None:
                echecs.append("lire_pdf ne retrouve rien")
            elif e["etat"] != "absent":
                echecs.append(f"etat : {e['etat']}")

            # le nom est unique, l'empreinte ne l'est pas
            try:
                db.enregistrer_pdf(con, "Labyrinth.pdf", "abc123")
                echecs.append("le nom devrait lever Doublon")
            except db.Doublon:
                pass
            # ... mais un autre nom, meme empreinte, passe
            db.enregistrer_pdf(con, "Copie.pdf", "abc123")

        # --- pdf_par_noms retrouve un PDF par un ancien nom
        with db.connecter(proj) as con:
            pid = db.lire_pdf(con, pid)["id"]
            db.renommer_pdf(con, pid, "Labyrinth of the Bride.pdf")
            if db.pdf_par_nom(con, "Labyrinth.pdf") is None:
                echecs.append("l'ancien nom doit rester trouvable")
            if db.pdf_par_nom(con, "Labyrinth of the Bride.pdf") is None:
                echecs.append("le nouveau nom doit etre trouvable")

        # --- recalculer : NULL = heritage du defaut global
        with db.connecter(proj) as con:
            pid2 = db.enregistrer_pdf(con, "Autre.pdf", "def456")
            if db.lire_recalculer(con, pid2) is not None:
                echecs.append("recalculer doit commencer a NULL")
            db.definir_recalculer(con, pid2, True)
            if db.lire_recalculer(con, pid2) is not True:
                echecs.append("recalculer = True non enregistre")
            db.definir_recalculer(con, pid2, None)
            if db.lire_recalculer(con, pid2) is not None:
                echecs.append("recalculer doit revenir a NULL")

        # --- une base corrompue donne BaseInvalide, pas n'importe quoi
        (proj / "analyse" / "etat.db").write_bytes(b"pas une base")
        try:
            with db.connecter(proj) as con:
                con.execute("SELECT 1 FROM pdf").fetchall()
        except db.BaseInvalide:
            pass
        except Exception as e:  # noqa: BLE001
            echecs.append(f"corrompu leve {type(e).__name__} au lieu de BaseInvalide")

    return _fin(echecs)


if __name__ == "__main__":
    main()