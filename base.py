#!/usr/bin/env python3
"""Etat d'un projet de traduction, dans une base SQLite.

Une base par projet : Projets/<nom>/analyse/etat.db. Elle est la SEULE source
de verite — les CSV n'existent que comme export temporaire pour BabelDOC, qui
ne sait lire que ca.

Le schema tourne autour d'une idee : tout se rattache au PDF par son
EMPREINTE, jamais par son nom. Le nom peut changer, l'identite non.

Aucun LLM, aucun reseau.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
PROJETS = RACINE / "Projets"
BASE = "analyse/etat.db"
EMPREINTE_LONGUEUR = 12_000  # caracteres pris en compte


class BaseInvalide(Exception):
    """La base est corrompue ou d'une version incompatible."""


class Doublon(Exception):
    """Un nom de PDF est deja connu du projet."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS pdf (
    id          INTEGER PRIMARY KEY,
    nom         TEXT NOT NULL,
    empreinte   TEXT,
    etat        TEXT NOT NULL DEFAULT 'present',
    mode        TEXT NOT NULL DEFAULT 'inclus',
    recalculer  INTEGER,
    vue         TEXT,
    ancien_noms TEXT NOT NULL DEFAULT '[]'
);
CREATE UNIQUE INDEX IF NOT EXISTS pdf_nom ON pdf(nom);
CREATE INDEX IF NOT EXISTS pdf_empreinte ON pdf(empreinte);

CREATE TABLE IF NOT EXISTS pdf_noms (
    pdf_id INTEGER NOT NULL REFERENCES pdf(id) ON DELETE CASCADE,
    nom    TEXT NOT NULL,
    PRIMARY KEY (pdf_id, nom)
);

CREATE TABLE IF NOT EXISTS contexte (
    id          INTEGER PRIMARY KEY,
    pdf_id      INTEGER NOT NULL REFERENCES pdf(id) ON DELETE CASCADE,
    resume      TEXT,
    points      TEXT NOT NULL DEFAULT '[]',
    termes_cles TEXT NOT NULL DEFAULT '[]',
    maj         TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS contexte_pdf ON contexte(pdf_id);

CREATE TABLE IF NOT EXISTS page_contexte (
    contexte_id INTEGER NOT NULL REFERENCES contexte(id) ON DELETE CASCADE,
    numero      INTEGER NOT NULL,
    resume      TEXT,
    PRIMARY KEY (contexte_id, numero)
);

CREATE TABLE IF NOT EXISTS lot (
    id    INTEGER PRIMARY KEY,
    points TEXT NOT NULL DEFAULT '[]',
    maj   TEXT
);

CREATE TABLE IF NOT EXISTS glossaire (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL UNIQUE,
    target      TEXT NOT NULL,
    tgt_lng     TEXT NOT NULL DEFAULT 'fr',
    origine     TEXT NOT NULL DEFAULT 'auto',
    occurrences INTEGER NOT NULL DEFAULT 0,
    nb_pages    INTEGER NOT NULL DEFAULT 0,
    maj         TEXT
);

CREATE TABLE IF NOT EXISTS page_glossaire (
    glossaire_id INTEGER NOT NULL REFERENCES glossaire(id) ON DELETE CASCADE,
    pdf_id       INTEGER NOT NULL REFERENCES pdf(id) ON DELETE CASCADE,
    page         INTEGER NOT NULL,
    PRIMARY KEY (glossaire_id, pdf_id, page)
);
CREATE INDEX IF NOT EXISTS pg_termes ON page_glossaire(glossaire_id);
CREATE INDEX IF NOT EXISTS pg_pdf ON page_glossaire(pdf_id);

CREATE TABLE IF NOT EXISTS police (
    id             INTEGER PRIMARY KEY,
    police_origine TEXT NOT NULL UNIQUE,
    remplacement   TEXT NOT NULL DEFAULT '',
    origine        TEXT NOT NULL DEFAULT 'defaut',
    propose        TEXT NOT NULL DEFAULT '',
    raison         TEXT NOT NULL DEFAULT '',
    famille        TEXT NOT NULL DEFAULT '',
    maj            TEXT
);

CREATE TABLE IF NOT EXISTS police_polices (
    police_id INTEGER NOT NULL REFERENCES police(id) ON DELETE CASCADE,
    pdf_id    INTEGER NOT NULL REFERENCES pdf(id) ON DELETE CASCADE,
    spans     INTEGER NOT NULL DEFAULT 0,
    pages     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (police_id, pdf_id)
);
CREATE INDEX IF NOT EXISTS pp_police ON police_polices(police_id);
CREATE INDEX IF NOT EXISTS pp_pdf ON police_polices(pdf_id);
"""


def chemin_base(projet: Path) -> Path:
    return projet / BASE


def projet(nom: str | None = None) -> Path | None:
    """Le projet : PDF2ZH_PROJET, sinon le premier trouve."""
    n = nom or os.environ.get("PDF2ZH_PROJET")
    if n:
        p = PROJETS / n
        if p.is_dir():
            return p
        print(f"projet '{n}' introuvable dans {PROJETS}", file=sys.stderr)
        return None
    if PROJETS.is_dir():
        for d in sorted(PROJETS.iterdir()):
            if d.is_dir() and not d.name.startswith("."):
                return d
    return None


@contextlib.contextmanager
def connecter(projet: Path):
    """Connexion, schema cree au besoin. Erreur claire si la base est corrompue."""
    f = chemin_base(projet)
    f.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(f), timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        con.executescript(SCHEMA)
    except sqlite3.DatabaseError as e:
        con.close()
        raise BaseInvalide(f"{f} illisible ou incompatible : {e}") from e
    try:
        yield con
        con.commit()
    finally:
        con.close()


def maintenant() -> str:
    return datetime.now().isoformat(timespec="seconds")


def empreinte_pdf(pdf: Path) -> str:
    """Empreinte du TEXTE normalise, pas des octets.

    Deux exports du meme document different au niveau octet mais ont le meme
    texte : c'est le texte qui identifie le document, et le seul moyen de
    reconnaitre un renommage ou un re-export.
    """
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf  # type: ignore
        except ImportError:
            return ""
    morceaux: list[str] = []
    try:
        with pymupdf.open(str(pdf)) as doc:
            for page in doc:
                morceaux.append(page.get_text())
                if sum(len(m) for m in morceaux) >= EMPREINTE_LONGUEUR:
                    break
    except Exception:  # noqa: BLE001 - un PDF illisible n'a pas d'identite
        return ""
    texte = " ".join(morceaux)[:EMPREINTE_LONGUEUR]
    texte = re.sub(r"\s+", " ", texte).strip().lower()
    return hashlib.sha256(texte.encode("utf-8")).hexdigest() if texte else ""


def enregistrer_pdf(con, nom: str, empreinte: str) -> int:
    """Enregistre un PDF vu. Le nom est unique, l'empreinte ne l'est pas."""
    if con.execute("SELECT 1 FROM pdf WHERE nom = ?", (nom,)).fetchone():
        raise Doublon(nom)
    cur = con.execute(
        "INSERT INTO pdf (nom, empreinte, etat, mode, vue, ancien_noms)"
        " VALUES (?, ?, 'present', 'inclus', ?, '[]')",
        (nom, empreinte, maintenant()),
    )
    pid = int(cur.lastrowid)
    con.execute(
        "INSERT OR IGNORE INTO pdf_noms (pdf_id, nom) VALUES (?, ?)", (pid, nom)
    )
    return pid


def mettre_pdf_etat(con, pdf_id: int, etat: str) -> None:
    con.execute(
        "UPDATE pdf SET etat = ?, vue = ? WHERE id = ?",
        (etat, maintenant(), pdf_id),
    )


def lire_pdf(con, pdf_id: int) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM pdf WHERE id = ?", (pdf_id,)).fetchone()


def pdf_par_nom(con, nom: str) -> sqlite3.Row | None:
    """Le PDF par son nom ACTUEL ou par un nom qu'il a porte.

    C'est ce qui fait qu'un PDF renomme retrouve ses donnees.
    """
    return con.execute(
        "SELECT p.* FROM pdf p JOIN pdf_noms n ON n.pdf_id = p.id"
        " WHERE n.nom = ? LIMIT 1",
        (nom,),
    ).fetchone()


def pdf_par_empreinte(con, empreinte: str) -> sqlite3.Row | None:
    if not empreinte:
        return None
    return con.execute(
        "SELECT * FROM pdf WHERE empreinte = ? LIMIT 1", (empreinte,)
    ).fetchone()


def renommer_pdf(con, pdf_id: int, nouveau: str) -> None:
    """Change le nom. L'ancien est conserve dans pdf_noms et ancien_noms."""
    ancien = lire_pdf(con, pdf_id)
    if ancien is None:
        raise KeyError(pdf_id)
    if con.execute(
        "SELECT 1 FROM pdf WHERE nom = ? AND id <> ?", (nouveau, pdf_id)
    ).fetchone():
        raise Doublon(nouveau)
    noms = json.loads(ancien["ancien_noms"] or "[]")
    if ancien["nom"] not in noms:
        noms.append(ancien["nom"])
    con.execute(
        "UPDATE pdf SET nom = ?, ancien_noms = ? WHERE id = ?",
        (nouveau, json.dumps(noms, ensure_ascii=False), pdf_id),
    )
    con.execute(
        "INSERT OR IGNORE INTO pdf_noms (pdf_id, nom) VALUES (?, ?)",
        (pdf_id, nouveau),
    )


# SQLite n'a pas de booleen : il stocke 0 et 1. On convertit a la lecture
# pour que l'appelant ait un vrai booleen, et NULL reste NULL (heritage).
def lire_recalculer(con, pdf_id: int):
    """None = herite du defaut global. True/False = surcharge explicite.

    SQLite n'a pas de booleen (il stocke 0/1) : la conversion est faite ici,
    une fois, plutot que dans chaque appelant.
    """
    row = lire_pdf(con, pdf_id)
    if row is None:
        return None
    v = row["recalculer"]
    return None if v is None else bool(v)


def definir_recalculer(con, pdf_id: int, valeur: bool | None) -> None:
    con.execute("UPDATE pdf SET recalculer = ? WHERE id = ?", (valeur, pdf_id))


def _auto_test() -> int:
    p = projet()
    if p is None:
        print(f"aucun projet dans {PROJETS}")
        return 0
    print(f"projet : {p.name}")
    with connecter(p) as con:
        n = con.execute("SELECT COUNT(*) FROM pdf").fetchone()[0]
        print(f"  {n} PDF connu(s)")
        for r in con.execute("SELECT nom, etat, mode FROM pdf ORDER BY nom"):
            marque = {"present": "OK ", "absent": "-- "}.get(r["etat"], "?  ")
            print(f"  {marque} {r['nom'][:44]:46} mode={r['mode']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_auto_test())