#!/usr/bin/env python3
"""Mise a jour du registre des PDF a partir du disque.

Ce module tient les deux promesses centrales du projet :

  - un PDF supprime de source/ garde son entree ET ses donnees ; il passe
    juste en 'absent', et l'utilisateur decide de l'ignorer ou de le purger
  - un PDF renomme a la main est reconnu par son empreinte, et peut etre
    rattache a ses anciennes donnees

Le mode (inclus / ignore) est une decision de l'utilisateur, preservee
d'une synchronisation a l'autre.

Aucune ecriture de fichier : tout passe par la base.
"""
from __future__ import annotations

from pathlib import Path

import base as db


def _pdfs_source(projet: Path) -> dict[str, Path]:
    d = projet / "source"
    return {p.name: p for p in sorted(d.glob("*.pdf"))} if d.is_dir() else {}


def synchroniser(con, projet: Path) -> dict:
    """Aligne la base sur le disque.

    Renvoie {'presents', 'absents', 'nouveaux'}, des noms de fichiers.
    Idempotent : appeler deux fois de suite ne change rien.
    """
    presents = _pdfs_source(projet)
    noms_base = {r["nom"] for r in con.execute("SELECT nom FROM pdf")}

    # 1. passage en absent : l'entree reste, seule l'etat bouge
    absents = noms_base - set(presents)
    for nom in sorted(absents):
        row = db.pdf_par_nom(con, nom)
        if row is not None and row["etat"] != "absent":
            db.mettre_pdf_etat(con, row["id"], "absent")

    # 2. nouveaux PDF
    nouveaux = set(presents) - noms_base
    for nom in sorted(nouveaux):
        try:
            db.enregistrer_pdf(con, nom, db.empreinte_pdf(presents[nom]))
        except db.Doublon:
            continue  # deja connu sous un ancien nom

    # 3. retour sur disque : l'etat redevient present, le mode est conserve
    for nom in sorted(noms_base & set(presents)):
        row = db.pdf_par_nom(con, nom)
        if row is not None and row["etat"] != "present":
            db.mettre_pdf_etat(con, row["id"], "present")

    return {
        "presents": sorted(presents),
        "absents": sorted(absents),
        "nouveaux": sorted(nouveaux),
    }


def reassociations(con) -> dict[str, tuple[int, str]]:
    """PDF present dont l'empreinte correspond a un PDF absent.

    Renvoie {nom_actuel: (id_du_ancien, ancien_nom)}. C'est a l'utilisateur
    de decider s'il rattache.
    """
    absents = {
        r["empreinte"]: r
        for r in con.execute(
            "SELECT id, nom, empreinte FROM pdf"
            " WHERE etat = 'absent' AND empreinte IS NOT NULL AND empreinte <> ''"
        )
    }
    if not absents:
        return {}

    out: dict[str, tuple[int, str]] = {}
    for r in con.execute(
        "SELECT id, nom, empreinte FROM pdf"
        " WHERE etat = 'present' AND empreinte IS NOT NULL AND empreinte <> ''"
    ):
        ancien = absents.get(r["empreinte"])
        if ancien is not None and ancien["id"] != r["id"]:
            out[r["nom"]] = (ancien["id"], ancien["nom"])
    return out


def rattacher(con, nouveau: str, ancien_id: int) -> None:
    """Le PDF 'nouveau' reprend l'identite de l'ancien.

    Tout ce qui etait rattache (contexte, pages, termes, polices) suit
    automatiquement : les tables referencent des id, pas des noms. C'est tout
    l'interet du modele.
    """
    nouveau_row = con.execute("SELECT id FROM pdf WHERE nom = ?", (nouveau,)).fetchone()
    if nouveau_row is None:
        raise KeyError(nouveau)
    ancien_row = lire_par_id(con, ancien_id)
    if ancien_row is None:
        raise KeyError(ancien_id)

    noms = [r["nom"] for r in con.execute(
        "SELECT nom FROM pdf_noms WHERE pdf_id = ?", (ancien_id,)
    )]
    if nouveau not in noms:
        noms.append(nouveau)

    # le nouvel entrant disparait, l'ancien prend son nom et ses noms passes
    con.execute("DELETE FROM pdf WHERE id = ?", (nouveau_row["id"],))
    con.execute("UPDATE pdf SET nom = ? WHERE id = ?", (nouveau, ancien_id))
    con.execute("DELETE FROM pdf_noms WHERE pdf_id = ?", (ancien_id,))
    for n in noms:
        con.execute(
            "INSERT OR IGNORE INTO pdf_noms (pdf_id, nom) VALUES (?, ?)",
            (ancien_id, n),
        )


def lire_par_id(con, pdf_id: int):
    return db.lire_pdf(con, pdf_id)


def definir_mode(con, nom: str, mode: str) -> None:
    """'inclus' ou 'ignore'. Un PDF ignore garde ses donnees, il est seulement
    exclu des prochaines analyses."""
    if mode not in {"inclus", "ignore"}:
        raise ValueError(f"mode inconnu : {mode}")
    con.execute("UPDATE pdf SET mode = ? WHERE nom = ?", (mode, nom))


def pdf_a_analyser(con) -> list[str]:
    """Noms des PDF presents ET inclus, tries."""
    return [
        r["nom"] for r in con.execute(
            "SELECT nom FROM pdf WHERE etat = 'present' AND mode = 'inclus' ORDER BY nom"
        )
    ]


def pdf_a_purger(con, nom: str) -> dict:
    """Ce que la suppression de ce PDF ferait perdre.

    Renvoie un resume, pour que l'interface puisse l'afficher AVANT de
    supprimer. Supprimer est definitif : mieux vaut voir.
    """
    row = db.pdf_par_nom(con, nom)
    if row is None:
        return {"connu": False}

    pid = row["id"]
    ctx = con.execute("SELECT id FROM contexte WHERE pdf_id = ?", (pid,)).fetchone()
    pages = 0
    if ctx is not None:
        pages = con.execute(
            "SELECT COUNT(*) FROM page_contexte WHERE contexte_id = ?", (ctx["id"],)
        ).fetchone()[0]

    return {
        "connu": True,
        "id": pid,
        "nom": nom,
        "etat": row["etat"],
        "mode": row["mode"],
        "contexte": bool(ctx),
        "pages_contexte": pages,
        "termes": con.execute(
            "SELECT COUNT(DISTINCT glossaire_id) FROM page_glossaire WHERE pdf_id = ?",
            (pid,),
        ).fetchone()[0],
        "polices": con.execute(
            "SELECT COUNT(DISTINCT police_id) FROM police_polices WHERE pdf_id = ?",
            (pid,),
        ).fetchone()[0],
        "anciens": __import__("json").loads(row["ancien_noms"] or "[]"),
    }


def supprimer_donnees(con, nom: str) -> dict:
    """Supprime le CONTEXTE de ce PDF, et uniquement lui.

    Le PDF reste au registre : on peut le purger separement. Ses termes de
    glossaire et ses polices ne sont PAS touches — ils peuvent venir d'autres
    PDF, et deviendront orphelins si besoin.
    """
    avant = pdf_a_purger(con, nom)
    if not avant["connu"]:
        raise KeyError(nom)
    con.execute(
        "DELETE FROM page_contexte WHERE contexte_id IN"
        " (SELECT id FROM contexte WHERE pdf_id = ?)",
        (avant["id"],),
    )
    con.execute("DELETE FROM contexte WHERE pdf_id = ?", (avant["id"],))
    return {"nom": nom, "pages_supprimees": avant["pages_contexte"]}


def supprimer_entree(con, nom: str) -> dict:
    """Supprime la FICHE du PDF. Ses donnees siguen et deviendront orphelines.

    Definitif : le confirmeur doit avoir montre ce qui sera perdu.
    """
    avant = pdf_a_purger(con, nom)
    if not avant["connu"]:
        raise KeyError(nom)
    # ON DELETE CASCADE emporte contexte, pages, termes et polices de CE pdf
    con.execute("DELETE FROM pdf WHERE id = ?", (avant["id"],))
    return {"nom": nom, "contexte_perdu": avant["pages_contexte"]}


def _auto_test() -> int:
    p = db.projet()
    if p is None:
        print(f"aucun projet")
        return 0
    print(f"projet : {p.name}")
    with db.connecter(p) as con:
        r = synchroniser(con, p)
        print(f"  {len(r['presents'])} present(s), {len(r['absents'])} absent(s), "
              f"{len(r['nouveaux'])} nouveau(x)")
        for row in con.execute(
            "SELECT nom, etat, mode, COALESCE(empreinte,'') AS e FROM pdf ORDER BY nom"
        ):
            marque = {"present": "OK ", "absent": "-- "}.get(row["etat"], "?  ")
            print(f"  {marque} {row['nom'][:42]:44} mode={row['mode']:7} emp={row['e'][:8]}")
        props = reassociations(con)
        for nouveau, (_id, ancien) in props.items():
            print(f"  ?? {nouveau[:42]:44} reassocier a {ancien}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_auto_test())