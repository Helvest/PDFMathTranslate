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


def chemins(projet: Path | None = None) -> tuple[Path, Path]:
    """(csv, dossier des TTF) pour un projet. (None, None) si aucun projet."""
    p = projet or projet_courant()
    if p is None:
        return Path(), Path()
    return p / "analyse" / "polices.csv", p / "analyse" / "polices"

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


def _resoudre(fichier: str, dossier: Path) -> Path | None:
    """Trouve le TTF : chemin absolu, relatif au dossier, ou nom nu."""
    if not fichier:
        return None
    p = Path(fichier)
    candidats = []
    if p.is_absolute():
        candidats.append(p)
    else:
        candidats.append(dossier / p)
        candidats.append(REPO / p)
    # si l'extension manque, on essaie .ttf puis .otf
    if p.suffix.lower() not in EXTS:
        candidats += [c.with_suffix(e) for c in list(candidats) for e in EXTS]
    for c in candidats:
        if c.is_file():
            return c
    return None


def _encoding_length(font: pymupdf.Font) -> int:
    """1 = simple (8 bits), 2 = CID (16 bits).

    BabelDOC s'en sert pour formater l'identifiant de glyphe en hexa :
    `f"<{char_id:0{encoding_length * 2}x}>"`. Une police TTF moderne a plus
    de 255 glyphes -> 2. On suit la meme regle que le frontend
    (il_creater.py:639-668 : >255 glyphes => 2).
    """
    return 2 if font.glyph_count > 255 else 1


def charger(
    csv_path: Path | str | None = None,
    dossier_ttf: Path | str | None = None,
    projet: Path | None = None,
) -> dict[str, PoliceCustom]:
    """Charge le mapping. Retourne {nom_origine: PoliceCustom}.

    Sans chemin explicite, on prend le projet courant (PDF2ZH_PROJET, sinon le
    premier trouve). Les lignes sans remplacement sont ignorees (detection auto
    de BabelDOC). Un fichier introuvable est signale et ignore, jamais fatal.
    """
    if csv_path is None or dossier_ttf is None:
        csv_defaut, ttf_defaut = chemins(projet)
        csv_path = csv_path or csv_defaut
        dossier_ttf = dossier_ttf or ttf_defaut

    csv_path = Path(csv_path)
    dossier_ttf = Path(dossier_ttf)

    if not csv_path.is_file():
        logger.debug("pas de mapping de polices : %s", csv_path)
        return {}

    resultat: dict[str, PoliceCustom] = {}
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        for ligne in csv.DictReader(f):
            nom = (ligne.get("police_origine") or "").strip()
            # la colonne s'appelle 'remplacement' (ancien nom : fichier_remplacement)
            fichier = (
                ligne.get("remplacement") or ligne.get("fichier_remplacement") or ""
            ).strip()
            if not nom or not fichier:
                continue

            chemin = _resoudre(fichier, dossier_ttf)
            if chemin is None:
                logger.warning(
                    "police introuvable pour %s : %s (cherche dans %s)",
                    nom,
                    fichier,
                    dossier_ttf,
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
    projet = projet_courant()
    if projet is None:
        print(f"aucun projet dans {PROJETS}")
        print("cree-en un :  analyser.py --projet <nom> --creer")
        return 0

    csv_defaut, ttf_defaut = chemins(projet)
    print(f"projet  : {projet.name}")
    print(f"mapping : {csv_defaut}")
    print(f"dossier : {ttf_defaut}")

    if not csv_defaut.is_file():
        print("  (pas de polices.csv -> rien a tester)")
        return 0

    polices = charger()
    print(f"\n{len(polices)} police(s) chargee(s) :")
    for nom, p in sorted(polices.items()):
        print(
            f"  {nom:26} -> {p.chemin.name:26} "
            f"asc={p.ascent:+.3f} desc={p.descent:+.3f} "
            f"enc={p.encoding_length} bold={int(p.bold)} italic={int(p.italic)}"
        )

    # controle : les valeurs doivent etre exploitables par BabelDOC
    for nom, p in polices.items():
        assert p.chemin.is_file(), f"{nom}: fichier absent"
        assert p.encoding_length in (1, 2), f"{nom}: encoding_length invalide"
        assert -2.0 < p.descent < 0, f"{nom}: descent hors plage ({p.descent})"
        assert 0 < p.ascent < 2.0, f"{nom}: ascent hors plage ({p.ascent})"
        assert p.font.glyph_count > 0, f"{nom}: aucune glyphe"

    print("\nOK : toutes les valeurs sont dans les plages attendues")
    return 0


if __name__ == "__main__":
    raise SystemExit(_auto_test())
