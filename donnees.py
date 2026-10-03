#!/usr/bin/env python3
"""Operations metier sur la base : glossaire, polices, orphelins.

Ce que l'interface consomme directement. Deux idees :

  - un terme ou une police n'est PAS orphelin tant qu'un seul PDF source
    survit ; l'orphelin est une REQUETE, pas un drapeau a maintenir
  - tout se lit a partir des tables many-to-many, donc une suppression de PDF
    se reflete automatiquement, sans balayer quoi que ce soit

Aucun LLM, aucun reseau.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import base as db


# ---------------------------------------------------------------- glossaire


def upsert_terme(con, source: str, target: str, tgt_lng: str = "fr",
                 origine: str = "auto", occurrences: int = 0,
                 nb_pages: int = 0, definition: str = "") -> int:
    """Cree ou met a jour un terme. Renvoie son id.

    Une cible modifiee a la main passe en origine='manuel' : c'est la meme
    regle que pour les polices.
    """
    row = con.execute("SELECT id, target FROM glossaire WHERE source = ?",
                      (source,)).fetchone()
    if row is None:
        cur = con.execute(
            "INSERT INTO glossaire (source, target, tgt_lng, origine,"
            " definition, occurrences, nb_pages, maj)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (source, target, tgt_lng, origine, definition, occurrences,
             nb_pages, db.maintenant()),
        )
        return int(cur.lastrowid)

    gid = int(row["id"])
    if row["target"] != target and origine == "auto":
        origine = "manuel"
    # une definition deja relue ne s'ecrase pas : elle vient d'un agent
    if definition:
        con.execute(
            "UPDATE glossaire SET target = ?, tgt_lng = ?, origine = ?,"
            " definition = ?, occurrences = ?, nb_pages = ?, maj = ? WHERE id = ?",
            (target, tgt_lng, origine, definition, occurrences, nb_pages,
             db.maintenant(), gid),
        )
    else:
        con.execute(
            "UPDATE glossaire SET target = ?, tgt_lng = ?, origine = ?,"
            " occurrences = ?, nb_pages = ?, maj = ? WHERE id = ?",
            (target, tgt_lng, origine, occurrences, nb_pages,
             db.maintenant(), gid),
        )
    return gid


def lier_pages_terme(con, glossaire_id: int, pdf_id: int, pages: list[int]) -> None:
    """Rattache un terme a des pages d'un PDF. Sans doublon."""
    for p in pages:
        con.execute(
            "INSERT OR IGNORE INTO page_glossaire (glossaire_id, pdf_id, page)"
            " VALUES (?, ?, ?)", (glossaire_id, pdf_id, int(p))
        )


def termes_du_pdf(con, pdf_id: int) -> set[str]:
    return {
        r["source"] for r in con.execute(
            "SELECT DISTINCT g.source FROM glossaire g"
            " JOIN page_glossaire pg ON pg.glossaire_id = g.id"
            " WHERE pg.pdf_id = ?", (pdf_id,)
        )
    }


# SQL commun : un orphelin est ce dont plus AUCUNE ligne ne pointe vers un pdf
# qui existe encore.
_ORPHELIN_TERME = """
SELECT g.* FROM glossaire g
WHERE NOT EXISTS (
    SELECT 1 FROM page_glossaire pg
    JOIN pdf p ON p.id = pg.pdf_id
    WHERE pg.glossaire_id = g.id
)
"""


def termes_orphelins(con):
    return con.execute(_ORPHELIN_TERME).fetchall()


def lister_termes(con, orphelins_seulement: bool = False) -> list[dict]:
    """Tous les termes, avec leurs sources et leurs pages.

    sources : liste de {pdf, etat, pages} — l'interface affiche les noms, et
    sait que 'absent' veut dire donnee orpheline.
    """
    sql = _ORPHELIN_TERME if orphelins_seulement else "SELECT g.* FROM glossaire g"
    out = []
    for r in con.execute(sql):
        sources = []
        # GROUP BY pdf : sans cela un terme present sur 3 pages du meme PDF
        # apparaitrait 3 fois dans la liste des sources.
        for s in con.execute(
            "SELECT p.id, p.nom, p.etat FROM page_glossaire pg"
            " JOIN pdf p ON p.id = pg.pdf_id WHERE pg.glossaire_id = ?"
            " GROUP BY p.id, p.nom, p.etat ORDER BY p.nom", (r["id"],)
        ):
            pages = [x["page"] for x in con.execute(
                "SELECT page FROM page_glossaire"
                " WHERE glossaire_id = ? AND pdf_id = ? ORDER BY page",
                (r["id"], s["id"]),
            )]
            sources.append({"pdf": s["nom"], "etat": s["etat"], "pages": pages})
        out.append({
            "id": r["id"], "source": r["source"], "target": r["target"],
            "tgt_lng": r["tgt_lng"], "origine": r["origine"],
            "occurrences": r["occurrences"], "nb_pages": r["nb_pages"],
            "definition": r["definition"] or "",
            "sources": sources,
            "orphelin": not sources,
        })
    return out


def purger_termes_orphelins(con) -> int:
    """Supprime les termes dont plus aucune source ne survit. Definitif."""
    ids = [int(r["id"]) for r in termes_orphelins(con)]
    for gid in ids:
        con.execute("DELETE FROM glossaire WHERE id = ?", (gid,))
    return len(ids)


# ------------------------------------------------------------------ polices


def upsert_police(con, police_origine: str, remplacement: str = "",
                  origine: str = "defaut", propose: str = "",
                  raison: str = "", famille: str = "") -> int:
    row = con.execute("SELECT id FROM police WHERE police_origine = ?",
                      (police_origine,)).fetchone()
    if row is None:
        cur = con.execute(
            "INSERT INTO police (police_origine, remplacement, origine,"
            " propose, raison, famille, maj) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (police_origine, remplacement, origine, propose, raison, famille,
             db.maintenant()),
        )
        return int(cur.lastrowid)
    pid = int(row["id"])
    con.execute(
        "UPDATE police SET remplacement = ?, origine = ?, propose = ?,"
        " raison = ?, famille = ?, maj = ? WHERE id = ?",
        (remplacement, origine, propose, raison, famille, db.maintenant(), pid),
    )
    return pid


def lier_police(con, police_id: int, pdf_id: int, spans: int, pages: int) -> None:
    con.execute(
        "INSERT INTO police_polices (police_id, pdf_id, spans, pages)"
        " VALUES (?, ?, ?, ?) ON CONFLICT(police_id, pdf_id)"
        " DO UPDATE SET spans = excluded.spans, pages = excluded.pages",
        (police_id, pdf_id, spans, pages),
    )


_ORPHELIN_POLICE = """
SELECT po.* FROM police po
WHERE NOT EXISTS (
    SELECT 1 FROM police_polices pp
    JOIN pdf p ON p.id = pp.pdf_id
    WHERE pp.police_id = po.id
)
"""


def polices_orphelines(con):
    return con.execute(_ORPHELIN_POLICE).fetchall()


def lister_polices(con, orphelines_seulement: bool = False) -> list[dict]:
    """Toutes les polices, avec leur usage cumule et leurs sources.

    spans et pages sont cumules sur tous les PDF — c'est ce que l'interface
    affiche dans la colonne « usage ».
    """
    sql = _ORPHELIN_POLICE if orphelines_seulement else "SELECT po.* FROM police po"
    out = []
    for r in con.execute(sql):
        sources = []
        for s in con.execute(
            "SELECT p.id, p.nom, p.etat, pp.spans, pp.pages"
            " FROM police_polices pp JOIN pdf p ON p.id = pp.pdf_id"
            " WHERE pp.police_id = ? ORDER BY p.nom", (r["id"],)
        ):
            sources.append({
                "pdf": s["nom"], "etat": s["etat"],
                "spans": s["spans"], "pages": s["pages"],
            })
        out.append({
            "id": r["id"], "police_origine": r["police_origine"],
            "remplacement": r["remplacement"], "origine": r["origine"],
            "propose": r["propose"], "raison": r["raison"],
            "famille": r["famille"],
            "spans": sum(s["spans"] for s in sources),
            "pages": sum(s["pages"] for s in sources),
            "nb_pdfs": len(sources),
            "sources": sources,
            "orpheline": not sources,
        })
    return out


def purger_polices_orphelines(con) -> int:
    ids = [int(r["id"]) for r in polices_orphelines(con)]
    for pid in ids:
        con.execute("DELETE FROM police WHERE id = ?", (pid,))
    return len(ids)


# ------------------------------------------------------------------ export


def exporter_glossaire_csv(con, chemin: Path) -> Path:
    """Le CSV que BabelDOC lit (glossary.py : source, target, target_language).

    C'est le SEUL endroit ou un CSV existe. Il est regenere a chaque
    traduction puis supprime : aucune divergence possible avec la base.
    """
    chemin.parent.mkdir(parents=True, exist_ok=True)
    with chemin.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["source", "target", "tgt_lng"])
        for r in con.execute(
            "SELECT source, target, tgt_lng FROM glossaire"
            " WHERE source <> '' AND target <> '' ORDER BY lower(source)"
        ):
            w.writerow([r["source"], r["target"], r["tgt_lng"]])
    return chemin


def exporter_polices_csv(con, chemin: Path) -> Path:
    """Export des polices, pour inspection. Ce n'est pas lu par BabelDOC."""
    chemin.parent.mkdir(parents=True, exist_ok=True)
    with chemin.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["police_origine", "remplacement", "origine", "propose",
                    "raison", "famille", "spans", "pages", "nb_pdfs"])
        for r in lister_polices(con):
            w.writerow([r["police_origine"], r["remplacement"], r["origine"],
                        r["propose"], r["raison"], r["famille"],
                        r["spans"], r["pages"], r["nb_pdfs"]])
    return chemin


# ------------------------------------------------------------------ compte


def compter(con) -> dict:
    """Les compteurs de l'onglet 'etape suivante'."""
    def n(sql: str, params=()) -> int:
        return int(con.execute(sql, params).fetchone()[0])

    return {
        "pdfs": n("SELECT COUNT(*) FROM pdf"),
        "pdfs_present": n("SELECT COUNT(*) FROM pdf WHERE etat='present'"),
        "pdfs_absents": n("SELECT COUNT(*) FROM pdf WHERE etat='absent'"),
        "termes": n("SELECT COUNT(*) FROM glossaire"),
        "termes_orphelins": len(termes_orphelins(con)),
        "polices": n("SELECT COUNT(*) FROM police"),
        "polices_orphelines": len(polices_orphelines(con)),
        "contextes": n("SELECT COUNT(*) FROM contexte"),
    }