#!/usr/bin/env python3
"""Polices custom pour BabelDOC.

Lit le mapping manuel produit par analyser.py :

    Projets/<projet>/analyse/polices.csv    police_origine,remplacement,origine,...
    Projets/<projet>/analyse/polices/       les .ttf de remplacement

Le projet se choisit par la variable PDF2ZH_PROJET (positionnee par
traduire.sh), sinon le premier projet trouve.

Pourquoi ce module existe : BabelDOC n'offre aucun moyen de fournir ses
propres polices (le parametre TranslationConfig.font est neutralise :
`self.font = None  # just ignore font`). On injecte donc nos polices dans
l'instance de FontMapper apres son initialisation, ce qui evite au passage
la validation verify_font_family() et la verification SHA3-256.

Aucun LLM ici : tout est deterministe.
"""
from __future__ import annotations

import csv
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import pymupdf

import base
import catalogue_polices as catalogue

logger = logging.getLogger(__name__)

# Les donnees vivent hors du depot, un dossier par projet.
REPO = Path(__file__).resolve().parent
PROJETS = REPO.parent / "Projets"


def projet_courant() -> Path | None:
    """Le projet a utiliser : PDF2ZH_PROJET, sinon le premier trouve.

    Retourne None si aucun projet n'existe, pour que les appelants puissent
    continuer sans polices custom plutot que de planter.
    """
    nom = os.environ.get("PDF2ZH_PROJET")
    if nom:
        p = PROJETS / nom
        if p.is_dir():
            return p
        logger.warning("projet '%s' introuvable dans %s", nom, PROJETS)
    if PROJETS.is_dir():
        for d in sorted(PROJETS.iterdir()):
            if d.is_dir() and not d.name.startswith("."):
                return d
    return None


DOSSIER_TTF_DEFAUT: Path | None = None  # calcule a l'appel (le projet peut changer)


# Extensions acceptees pour un fichier de remplacement.
EXTS = (".ttf", ".otf")


@dataclass
class PoliceCustom:
    """Une police de remplacement, prete pour BabelDOC."""

    nom_origine: str  # cle dans polices.csv, ex "FuturaPT-Book"
    famille: str  # ex "FuturaPT"
    chemin: Path  # le .ttf resolu
    font: pymupdf.Font  # objet PyMuPDF, avec les attributs attendus
    ascent: float
    descent: float
    encoding_length: int
    bold: bool
    italic: bool
    serif: bool
    monospace: bool


def _mapping_global(con) -> list[dict]:
    """Les remplacements declares dans Global/polices.csv.

    C'est le seul CSV qui subsiste : il est partage entre tous les projets et
    modifie a la main. L'etat.db du projet prime — une ligne de la base sans
    remplacement n'est pas un choix, donc le global peut la completer.
    """
    csv_global = catalogue.CSV_GLOBAL
    if not csv_global.is_file():
        return []
    out = []
    try:
        with csv_global.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                nom = (row.get("police_origine") or "").strip()
                fichier = (row.get("remplacement") or "").strip()
                if nom and fichier:
                    out.append({
                        "police_origine": nom,
                        "remplacement": fichier,
                        "famille": (row.get("famille") or "").strip(),
                    })
    except (OSError, csv.Error) as e:
        logger.warning("mapping global illisible : %s", e)
    return out


def _resoudre(fichier: str, dossier: Path) -> Path | None:
    """Trouve le TTF dans les trois sources : projet, global, BabelDOC.

    Le dossier du projet est passe en premier (le plus specifique), puis le
    catalogue cherche dans le global et le cache de BabelDOC.
    """
    if not fichier:
        return None

    p = Path(fichier)
    if p.is_absolute():
        return p if p.is_file() else None

    # 1. le dossier du projet, explicitement (le catalogue peut ne pas le voir)
    candidats = [dossier / p]
    if p.suffix.lower() not in EXTS:
        candidats += [c.with_suffix(e) for c in list(candidats) for e in EXTS]
    for c in candidats:
        if c.is_file():
            return c

    # 2. puis le catalogue : global, puis BabelDOC, puis le depot
    chemin, _source = catalogue.resoudre(fichier)
    return chemin


def _encoding_length(font: pymupdf.Font) -> int:
    """1 = simple (8 bits), 2 = CID (16 bits).

    BabelDOC s'en sert pour formater l'identifiant de glyphe en hexa :
    `f"<{char_id:0{encoding_length * 2}x}>"`. Une police TTF moderne a plus
    de 255 glyphes -> 2. On suit la meme regle que le frontend
    (il_creater.py:639-668 : >255 glyphes => 2).
    """
    return 2 if font.glyph_count > 255 else 1


def charger(
    projet: Path | None = None,
    dossier_ttf: Path | str | None = None,
) -> dict[str, PoliceCustom]:
    """Le mapping de remplacement, lu dans etat.db.

    Retourne {police_origine: PoliceCustom}. Les polices sans remplacement
    sont ignorees : c'est la detection automatique de BabelDOC qui s'en
    charge. Un fichier introuvable est signale et ignore, jamais fatal.

    Le choix vient du projet si la base en porte un, sinon du global :
    modifier Global/polices.csv change donc tous les projets qui n'ont pas
    de decision propre.
    """
    proj = projet or projet_courant()
    if proj is None:
        return {}
    if dossier_ttf is None:
        dossier_ttf = proj / "analyse" / "polices"
    dossier_ttf = Path(dossier_ttf)

    lignes: list[dict] = []
    try:
        with base.connecter(proj) as con:
            lignes = [
                dict(r) for r in con.execute(
                    "SELECT police_origine, remplacement, famille FROM police"
                    " WHERE remplacement <> ''")
            ]
            for g in _mapping_global(con):
                # le projet gagne : une ligne sans remplacement n'est pas un choix
                lignes.append(dict(g))
    except base.BaseInvalide as e:
        logger.warning("base illisible (%s) — polices par defaut", e)
        return {}
    except Exception as e:  # noqa: BLE001 - ne jamais bloquer une traduction
        logger.warning("mapping illisible (%s) — polices par defaut", e)
        return {}

    resultat: dict[str, PoliceCustom] = {}
    for ligne in lignes:
        nom = (ligne.get("police_origine") or "").strip()
        fichier = (ligne.get("remplacement") or "").strip()
        if not nom or not fichier:
            continue

        chemin = _resoudre(fichier, dossier_ttf)
        if chemin is None:
            logger.warning(
                "police introuvable pour %s : %s (cherche dans %s)",
                nom, fichier, dossier_ttf,
            )
            continue

        try:
            font = pymupdf.Font(fontfile=str(chemin))
        except Exception as e:  # noqa: BLE001 - on ne veut jamais planter ici
            logger.warning("police illisible %s : %s", chemin, e)
            continue

        resultat[nom] = PoliceCustom(
            nom_origine=nom,
            famille=(ligne.get("famille") or "").strip(),
            chemin=chemin,
            font=font,
            ascent=font.ascender,
            descent=font.descender,
            encoding_length=_encoding_length(font),
            bold=bool(font.is_bold),
            italic=bool(font.is_italic),
            serif=bool(font.is_serif),
            monospace=bool(font.is_monospaced),
        )

    if resultat:
        logger.info("%d police(s) custom chargee(s)", len(resultat))
    return resultat


def _auto_test() -> int:
    """Verifie le chargement sur le mapping reel du projet."""
    proj = projet_courant()
    if proj is None:
        print(f"aucun projet")
        return 0

    print(f"projet  : {proj.name}")
    base_f = base.chemin_base(proj)
    print(f"base    : {base_f}")
    if not base_f.is_file():
        print("  (pas encore de base -> rien a tester)")
        return 0

    with base.connecter(proj) as con:
        total = con.execute("SELECT COUNT(*) FROM police").fetchone()[0]
        choisi = con.execute(
            "SELECT COUNT(*) FROM police WHERE remplacement <> ''").fetchone()[0]
    print(f"  {total} police(s) detectee(s), {choisi} avec remplacement")

    polices = charger()
    print(f"\n{len(polices)} police(s) chargee(s) :")
    for nom, pc in sorted(polices.items()):
        print(
            f"  {nom:26} -> {pc.chemin.name:26} "
            f"asc={pc.ascent:+.3f} desc={pc.descent:+.3f} "
            f"enc={pc.encoding_length} bold={int(pc.bold)} italic={int(pc.italic)}"
        )

    # controle : les valeurs doivent etre exploitables par BabelDOC
    for nom, pc in polices.items():
        assert pc.chemin.is_file(), f"{nom}: fichier absent"
        assert pc.encoding_length in (1, 2), f"{nom}: encoding_length invalide"
        assert -2.0 < pc.descent < 0, f"{nom}: descent hors plage ({pc.descent})"
        assert 0 < pc.ascent < 2.0, f"{nom}: ascent hors plage ({pc.ascent})"
        assert pc.font.glyph_count > 0, f"{nom}: aucune glyphe"

    print("\nOK : toutes les valeurs sont dans les plages attendues")
    return 0


if __name__ == "__main__":
    raise SystemExit(_auto_test())
