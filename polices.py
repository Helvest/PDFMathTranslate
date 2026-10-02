#!/usr/bin/env python3
"""Polices custom pour BabelDOC.

Lit le mapping manuel produit par analyser.py :

    ../Work/analyse/polices.csv          police_origine,famille,fichier_remplacement,source_pdf
    ../Work/analyse/polices/             les .ttf de remplacement

et le transforme en objets prets a etre injectes dans le FontMapper de
BabelDOC.

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
from dataclasses import dataclass
from pathlib import Path

import pymupdf

logger = logging.getLogger(__name__)

# Le dossier de travail est hors du depot (voir references/passe-analyse.md).
REPO = Path(__file__).resolve().parent
WORK = REPO.parent / "Work"
CSV_DEFAUT = WORK / "analyse" / "polices.csv"
DOSSIER_TTF_DEFAUT = WORK / "analyse" / "polices"

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
) -> dict[str, PoliceCustom]:
    """Charge le mapping. Retourne {nom_origine: PoliceCustom}.

    Les lignes sans fichier_remplacement sont ignorees (detection auto de
    BabelDOC). Un fichier introuvable est signale et ignore, jamais fatal.
    """
    csv_path = Path(csv_path) if csv_path else CSV_DEFAUT
    dossier_ttf = Path(dossier_ttf) if dossier_ttf else DOSSIER_TTF_DEFAUT

    if not csv_path.is_file():
        logger.debug("pas de mapping de polices : %s", csv_path)
        return {}

    resultat: dict[str, PoliceCustom] = {}
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        for ligne in csv.DictReader(f):
            nom = (ligne.get("police_origine") or "").strip()
            fichier = (ligne.get("fichier_remplacement") or "").strip()
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
    print(f"mapping : {CSV_DEFAUT}")
    print(f"dossier : {DOSSIER_TTF_DEFAUT}")

    if not CSV_DEFAUT.is_file():
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
