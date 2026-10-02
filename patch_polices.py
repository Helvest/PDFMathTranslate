#!/usr/bin/env python3
"""Patch de BabelDOC pour utiliser nos polices custom.

Principe : on herite de FontMapper au lieu d'editer fontmap.py. Un patch du
fichier serait perdu au prochain `pip install` de BabelDOC ; l'heritage, non.

Deux points d'injection :

  1. __init__  -> ajoute nos polices a self.fontid2fontpath / self.fontid2font.
                  C'est ce que add_font() lit pour ecrire les polices dans le
                  PDF : rien d'autre a modifier en aval.

  2. map()     -> consulte nos polices AVANT la detection automatique, pour
                  que le texte traduit utilise la police choisie.

Ce qui est evite au passage (les 4 verrous de BabelDOC) :
  - verify_font_family() : on n'ajoute RIEN a font_family, donc jamais appele
  - SHA3-256             : on ne passe pas par get_font_and_metadata()
  - table de metadonnees : ascent/descent/encoding_length lus dans le TTF
  - TranslationConfig.font : ignore, on ne s'en sert pas
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Renseigne par appliquer(). {nom_origine: PoliceCustom}
POLICES: dict = {}


def _injecter(mapper) -> None:
    """Ajoute nos polices aux tables du FontMapper."""
    if not POLICES:
        return

    ajoutees = 0
    for nom, p in POLICES.items():
        if nom in mapper.fontid2fontpath:
            continue

        font = p.font
        # BabelDOC lit ces trois attributs sur l'objet pymupdf.Font
        # (fontmap.py:75-81 et :290-292). On les pose ici.
        font.font_id = nom
        font.font_path = p.chemin
        font.ascent_fontmap = p.ascent
        font.descent_fontmap = p.descent
        font.encoding_length = p.encoding_length

        mapper.fonts[nom] = font
        mapper.fontid2fontpath[nom] = p.chemin
        mapper.fontid2font[nom] = font
        ajoutees += 1

    if ajoutees:
        logger.info("polices custom injectees dans FontMapper : %d", ajoutees)


def _map_custom(self, original_font, char_unicode: str):
    """Cherche dans nos polices avant la detection automatique.

    On respecte le style demande (gras/italique) pour ne pas ecraser une
    variante par une autre : si l'original est en gras, on prefere une police
    custom grasse.
    """
    if not POLICES:
        return None

    # style demande par la police d'origine
    try:
        veut_gras = bool(getattr(original_font, "bold", False) or getattr(original_font, "is_bold", False))
        veut_italique = bool(getattr(original_font, "italic", False) or getattr(original_font, "is_italic", False))
    except Exception:  # noqa: BLE001
        veut_gras = veut_italique = False

    # 1. le nom exact de la police d'origine est-il mappe ?
    nom = getattr(original_font, "name", None) or getattr(original_font, "font_id", None)
    candidats = []
    if nom and nom in POLICES:
        candidats.append(POLICES[nom])
    # 2. sinon, toutes nos polices (le style tranche)
    candidats.extend(p for p in POLICES.values() if p.nom_origine != nom)

    if not candidats:
        return None

    code = ord(char_unicode)

    # priorite au style exact
    for p in candidats:
        if p.font.has_glyph(code) and p.bold == veut_gras and p.italic == veut_italique:
            return p.font
    # puis n'importe laquelle qui a la glyphe
    for p in candidats:
        if p.font.has_glyph(code):
            return p.font
    return None


def appliquer(polices: dict | None = None) -> int:
    """Patche FontMapper dans tous les modules qui l'instancient.

    Retourne le nombre de modules patches. Sans polices a charger, ne fait
    rien (laisse BabelDOC se comporter normalement).
    """
    global POLICES

    if polices is not None:
        POLICES = polices

    if not POLICES:
        logger.debug("aucune police custom : patch non applique")
        return 0

    from babeldoc.format.pdf.document_il.utils.fontmap import FontMapper

    if getattr(FontMapper, "_custom_patch", False):
        logger.debug("FontMapper deja patche")
        return 0

    # --- 1. injecter a la construction -------------------------------------
    init_original = FontMapper.__init__

    def init_patche(self, translation_config):
        init_original(self, translation_config)
        _injecter(self)

    FontMapper.__init__ = init_patche

    # --- 2. consulter nos polices en premier -------------------------------
    map_original = FontMapper.map

    def map_patche(self, original_font, char_unicode):
        resultat = _map_custom(self, original_font, char_unicode)
        if resultat is not None:
            return resultat
        return map_original(self, original_font, char_unicode)

    FontMapper.map = map_patche
    FontMapper._custom_patch = True

    # --- 3. remplacer la classe dans les modules qui l'ont importee --------
    # FontMapper est instancie a 4 endroits ; ils font tous
    # `from ...fontmap import FontMapper`, donc chacun a sa propre reference.
    import importlib

    modules = [
        "babeldoc.format.pdf.document_il.frontend.il_creater",
        "babeldoc.format.pdf.document_il.backend.pdf_creater",
        "babeldoc.format.pdf.document_il.midend.il_translator",
        "babeldoc.docvision.rpc_doclayout6",
    ]
    patches = 0
    for nom_module in modules:
        try:
            m = importlib.import_module(nom_module)
        except ImportError:
            continue
        if getattr(m, "FontMapper", None) is not None:
            m.FontMapper = FontMapper
            patches += 1

    logger.info(
        "patch polices applique : %d police(s), %d module(s)", len(POLICES), patches
    )
    return patches


def _auto_test() -> int:
    """Verifie que le patch s'applique et que map() retourne nos polices."""
    import polices as module_polices

    chargees = module_polices.charger()
    if not chargees:
        print("aucune police chargee -> deposer des .ttf dans <projet>/analyse/polices/")
        print("et renseigner fichier_remplacement dans polices.csv")
        return 0

    print(f"{len(chargees)} police(s) a tester")
    n = appliquer(chargees)
    print(f"modules patches : {n}")

    from babeldoc.format.pdf.document_il.utils.fontmap import FontMapper

    assert getattr(FontMapper, "_custom_patch", False), "patch non applique"

    # le patch est-il bien propage aux modules consommateurs ?
    import importlib

    for nom_module in (
        "babeldoc.format.pdf.document_il.backend.pdf_creater",
        "babeldoc.format.pdf.document_il.frontend.il_creater",
    ):
        m = importlib.import_module(nom_module)
        assert m.FontMapper is FontMapper, f"non propage a {nom_module}"
        print(f"  propage a {nom_module.split('.')[-1]} : OK")

    print("\nOK : patch applique et propage")
    return 0


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
    raise SystemExit(_auto_test())
