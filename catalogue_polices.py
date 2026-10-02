#!/usr/bin/env python3
"""Catalogue des polices disponibles, toutes provenances confondues.

Trois sources, dans cet ordre de priorite :

    1. projet   Projets/<projet>/analyse/polices/   (+ son polices.csv)
    2. global   Global/polices/                     (+ son polices.csv)
    3. babeldoc le cache de BabelDOC (~/.cache/babeldoc/fonts/)

Le mapping se lit en CASCADE : une entree du projet gagne, sinon celle du
global, sinon rien (BabelDOC choisit alors sa police par defaut). Modifier
Global/polices.csv change donc le comportement de tous les projets qui n'ont
pas d'entree propre.

Ce module sert a deux choses :
  - fournir la liste des polices a l'interface (pour les listes deroulantes)
  - resoudre un fichier de remplacement, en cherchant dans les trois sources

Aucun LLM, aucun reseau : tout est local.
"""
from __future__ import annotations

import csv
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parent
RACINE = REPO.parent
PROJETS = RACINE / "Projets"
GLOBAL = RACINE / "Global"
POLICES_GLOBALES = GLOBAL / "polices"
CSV_GLOBAL = POLICES_GLOBALES / "polices.csv"

EXTS = (".ttf", ".otf", ".ttc")

# Le cache de BabelDOC : ses polices par defaut, toujours disponibles.
def cache_babeldoc() -> Path:
    """Dossier des polices embarquees par BabelDOC.

    BABELDOC_CACHE_DIR si defini, sinon ~/.cache/babeldoc (le defaut de
    BabelDOC, aussi utilise sous Windows).
    """
    env = os.environ.get("BABELDOC_CACHE_DIR")
    if env:
        return Path(env) / "fonts"
    return Path.home() / ".cache" / "babeldoc" / "fonts"


# Suffixes de style, pour regrouper une famille.
SUFFIXES = (
    "-BoldItalic", "-BoldOblique", "-Bold", "-Italic", "-Oblique", "-Regular",
    "-Light", "-Medium", "-Book", "-Thin", "-Black", "-ExtraBold", "-SemiBold",
)


def famille_de(nom: str) -> str:
    """'NotoSans-Bold' -> 'NotoSans'. Laisse le nom tel quel s'il n'a pas de style."""
    for s in SUFFIXES:
        if nom.endswith(s):
            return nom[: -len(s)]
    return nom


@dataclass
class Police:
    """Une police trouvee quelque part, avec sa provenance."""

    nom: str  # nom du fichier sans extension, ex "NotoSans-Bold"
    chemin: Path
    source: str  # "projet" | "global" | "babeldoc"
    famille: str = ""
    # le mapping (remplacement choisi pour une police d'origine) vit a part
    def __post_init__(self) -> None:
        if not self.famille:
            self.famille = famille_de(self.nom)

    @property
    def style(self) -> str:
        """'Bold', 'Italic', ''… pour l'affichage."""
        reste = self.nom[len(self.famille):].lstrip("-")
        return reste or "Regular"


@dataclass
class Catalogue:
    """Toutes les polices disponibles + le mapping en cascade."""

    polices: list[Police] = field(default_factory=list)
    # {police_origine: (fichier, origine, source_du_mapping)}
    mapping: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    erreurs: list[str] = field(default_factory=list)

    def par_nom(self, nom: str) -> Police | None:
        for p in self.polices:
            if p.nom == nom:
                return p
        return None

    def par_source(self, source: str) -> list[Police]:
        return [p for p in self.polices if p.source == source]

    def noms(self) -> list[str]:
        return [p.nom for p in self.polices]


def _lister_dossier(dossier: Path, source: str) -> list[Police]:
    """Les fichiers de police d'un dossier."""
    if not dossier.is_dir():
        return []
    out = []
    for f in sorted(dossier.iterdir()):
        if f.is_file() and f.suffix.lower() in EXTS:
            out.append(Police(nom=f.stem, chemin=f, source=source))
    return out


def _lire_csv(chemin: Path, source: str) -> dict[str, tuple[str, str, str]]:
    """Lit un polices.csv. {police_origine: (remplacement, origine, source)}."""
    out: dict[str, tuple[str, str, str]] = {}
    if not chemin.is_file():
        return out
    try:
        with chemin.open(encoding="utf-8-sig", newline="") as f:
            for ligne in csv.DictReader(f):
                nom = (ligne.get("police_origine") or "").strip()
                fichier = (
                    ligne.get("remplacement") or ligne.get("fichier_remplacement") or ""
                ).strip()
                if nom:
                    out[nom] = (fichier, (ligne.get("origine") or "").strip(), source)
    except (OSError, csv.Error) as e:
        logger.warning("polices.csv illisible (%s) : %s", chemin, e)
    return out


def projet_courant() -> Path | None:
    """Le projet a utiliser : PDF2ZH_PROJET, sinon le premier trouve."""
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


def charger(projet: Path | None = None) -> Catalogue:
    """Construit le catalogue complet, avec le mapping en cascade."""
    cat = Catalogue()

    # --- les fichiers, par provenance
    globales = _lister_dossier(POLICES_GLOBALES, "global")
    cache = _lister_dossier(cache_babeldoc(), "babeldoc")

    projet = projet or projet_courant()
    du_projet: list[Police] = []
    if projet is not None:
        du_projet = _lister_dossier(projet / "analyse" / "polices", "projet")

    # l'ordre compte : le projet d'abord, puis le global, puis babeldoc.
    # Un nom en double garde la premiere occurrence (la plus specifique).
    vus: set[str] = set()
    for groupe in (du_projet, globales, cache):
        for p in groupe:
            if p.nom in vus:
                continue
            vus.add(p.nom)
            cat.polices.append(p)

    # --- le mapping, en cascade : projet > global
    if projet is not None:
        for nom, val in _lire_csv(projet / "analyse" / "polices.csv", "projet").items():
            cat.mapping[nom] = val
    for nom, val in _lire_csv(CSV_GLOBAL, "global").items():
        if nom not in cat.mapping:
            cat.mapping[nom] = val

    return cat


def resoudre(fichier: str, projet: Path | None = None) -> tuple[Path | None, str]:
    """Trouve le fichier d'une police, dans les trois sources.

    Retourne (chemin, source). (None, "") si introuvable. On cherche d'abord
    dans le projet, puis le global, puis le cache de BabelDOC — comme pour la
    liste, du plus specifique au plus general.
    """
    if not fichier:
        return None, ""

    p = Path(fichier)
    # un chemin absolu, ou relatif au depot, est pris tel quel
    if p.is_absolute():
        return (p, "absolu") if p.is_file() else (None, "")

    candidats: list[tuple[Path, str]] = []
    projet = projet or projet_courant()
    if projet is not None:
        candidats.append((projet / "analyse" / "polices" / p, "projet"))
    candidats.append((POLICES_GLOBALES / p, "global"))
    candidats.append((cache_babeldoc() / p, "babeldoc"))
    candidats.append((REPO / p, "depot"))

    # sans extension, on essaie les extensions connues
    etendus: list[tuple[Path, str]] = []
    for c, src in candidats:
        etendus.append((c, src))
        if p.suffix.lower() not in EXTS:
            for e in EXTS:
                etendus.append((c.with_suffix(e), src))

    for c, src in etendus:
        if c.is_file():
            return c, src
    return None, ""


def _auto_test() -> int:
    """Montre le catalogue tel que le verra l'interface."""
    cat = charger()
    projet = projet_courant()

    print(f"projet   : {projet.name if projet else '(aucun)'}")
    print(f"global   : {POLICES_GLOBALES}")
    print(f"babeldoc : {cache_babeldoc()}")
    print()

    for src, libelle in [
        ("projet", "du projet"),
        ("global", "globales"),
        ("babeldoc", "de BabelDOC"),
    ]:
        liste = cat.par_source(src)
        print(f"=== {libelle} : {len(liste)} ===")
        for p in liste[:12]:
            print(f"  {p.nom:34} {p.famille:20} {p.style}")
        if len(liste) > 12:
            print(f"  … et {len(liste) - 12} autres")
        print()

    print(f"=== mapping ({len(cat.mapping)} entrees) ===")
    for nom, (fichier, origine, source) in sorted(cat.mapping.items()):
        etat = f"-> {fichier}" if fichier else "(aucun remplacement)"
        print(f"  {nom:26} {etat:30} [{origine or '?'} via {source}]")

    print(f"\ntotal : {len(cat.polices)} polices disponibles")
    return 0


if __name__ == "__main__":
    raise SystemExit(_auto_test())
