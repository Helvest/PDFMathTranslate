#!/usr/bin/env python3
"""Recherche et telechargement de polices depuis trois sources.

STRATEGIE EN CASCADE (demande utilisateur) :

    1. la VRAIE police      -> si on la trouve, on la prend
    2. une police SIMILAIRE -> sinon, une alternative libre proche
    3. rien                 -> fichier_remplacement reste vide, BabelDOC
                               utilisera sa police par defaut

SOURCES, dans l'ordre :

    Google Fonts   API JSON (1950 familles) + telechargement direct GitHub.
                   Fiable, pas de scraping. Cherche d'abord ici.
    dafontfree.io  scraping HTML. Beaucoup de choix, qualite variable.
    dafont.com     scraping HTML. Idem.

Tout ce qui est telecharge va dans Work/downloads/ et n'en bouge pas :
l'installation dans Work/analyse/polices/ reste un choix manuel.
"""
from __future__ import annotations

import html
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


def _llm(prompt: str) -> str:
    """Appel LLM via analyser.py (import paresseux : ce module reste
    utilisable seul, sans proxy Hermes)."""
    import analyser

    return analyser.llm(prompt)


def _parse_json_array(raw: str):
    import analyser

    return analyser.parse_json_array(raw)


REPO = Path(__file__).resolve().parent
DOWNLOADS = REPO.parent / "Work" / "downloads"
EXTS_POLICE = (".ttf", ".otf")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
TIMEOUT = 90

# Signatures d'un fichier de police valide.
MAGICS = (b"\x00\x01\x00\x00", b"OTTO", b"true", b"ttcf")


@dataclass
class Candidat:
    """Une police trouvee sur une source."""

    nom: str  # nom de la famille
    source: str  # google | dafontfree | dafont
    url: str  # page ou fichier
    exact: bool  # True si c'est la police demandee, False si similaire
    categorie: str = ""


@dataclass
class Resultat:
    demandee: str
    terme: str = ""
    candidat: Candidat | None = None
    archive: Path | None = None
    fichiers: list[Path] = field(default_factory=list)
    erreur: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.fichiers)

    @property
    def type_trouve(self) -> str:
        if not self.candidat:
            return "aucun"
        return "exacte" if self.candidat.exact else "similaire"


def _get(url: str, referer: str | None = None, binaire: bool = False):
    """GET avec User-Agent. Retourne texte ou bytes, None si echec."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    if referer:
        req.add_header("Referer", referer)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = r.read()
            return data if binaire else data.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        logger.debug("HTTP %s sur %s", e.code, url)
    except Exception as e:  # noqa: BLE001
        logger.debug("%s sur %s", type(e).__name__, url)
    return None


def _normaliser(s: str) -> str:
    """Pour comparer des noms de police : minuscules, sans espaces ni tirets."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


# --------------------------------------------------------------- Google Fonts

_GF_CACHE: list[dict] | None = None


def _google_fonts() -> list[dict]:
    """Catalogue Google Fonts (mis en cache pour la session)."""
    global _GF_CACHE
    if _GF_CACHE is not None:
        return _GF_CACHE
    raw = _get("https://fonts.google.com/metadata/fonts")
    if not raw:
        _GF_CACHE = []
        return _GF_CACHE
    try:
        _GF_CACHE = json.loads(raw).get("familyMetadataList", [])
    except json.JSONDecodeError:
        _GF_CACHE = []
    logger.debug("Google Fonts : %d familles", len(_GF_CACHE))
    return _GF_CACHE


def chercher_google(terme: str) -> list[Candidat]:
    """Cherche dans le catalogue Google Fonts."""
    familles = _google_fonts()
    if not familles:
        return []

    cible = _normaliser(terme)
    exacts, proches = [], []
    for f in familles:
        nom = f.get("family", "")
        n = _normaliser(nom)
        if not n:
            continue
        if n == cible:
            exacts.append(f)
        elif cible and (cible in n or n in cible):
            proches.append(f)

    # les plus populaires d'abord (popularity : petit = populaire)
    proches.sort(key=lambda f: f.get("popularity", 9999))

    def _cand(f: dict, exact: bool) -> Candidat:
        return Candidat(
            nom=f["family"],
            source="google",
            url=f"https://fonts.google.com/specimen/{urllib.parse.quote(f['family'])}",
            exact=exact,
            categorie=f.get("category", ""),
        )

    return [_cand(f, True) for f in exacts] + [_cand(f, False) for f in proches[:5]]


def _google_url_fichier(nom_famille: str) -> str | None:
    """URL du TTF sur le depot GitHub google/fonts.

    Le depot range par licence (ofl/, apache/, ufl/) et le nom de dossier est
    le nom de famille en minuscules sans espaces.
    """
    slug = _normaliser(nom_famille)
    colle = nom_famille.replace(" ", "")
    for licence in ("ofl", "apache", "ufl"):
        for motif in (
            f"{colle}%5Bwght%5D.ttf",  # police variable
            f"{colle}-Regular.ttf",
            f"{colle}.ttf",
        ):
            url = f"https://github.com/google/fonts/raw/main/{licence}/{slug}/{motif}"
            d = _get(url, binaire=True)
            if d and d[:4] in MAGICS:
                return url
    return None


# ------------------------------------------------------------- dafontfree.io

def chercher_dafontfree(terme: str) -> list[Candidat]:
    """Cherche sur dafontfree.io."""
    q = urllib.parse.quote_plus(terme)
    page = _get(f"https://www.dafontfree.io/?s={q}")
    if not page:
        return []

    exclure = {
        "feed", "contact", "about", "privacy-policy", "dmca", "disclaimer",
        "terms", "terms-of-service", "category", "tag", "author", "page",
        "wp-json", "comments", "cookie-policy", "refund-policy", "copyright",
        "faq", "blog", "news", "premium-fonts", "google-fonts",
    }
    cible = _normaliser(terme)
    vus, out = set(), []
    for url in re.findall(r'href="(https://www\.dafontfree\.io/[a-z0-9][a-z0-9-]*/)"', page):
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        if slug in exclure or slug in vus:
            continue
        vus.add(slug)
        nom = re.sub(r"-font$", "", slug).replace("-", " ").strip()
        out.append(
            Candidat(nom=nom, source="dafontfree", url=url, exact=_normaliser(nom) == cible)
        )
        if len(out) >= 6:
            break
    return out


def _dafontfree_lien(candidat: Candidat) -> str | None:
    """URL ?wpdmdl=<id> depuis la page d'une police."""
    page = _get(candidat.url)
    if not page:
        return None
    m = re.search(
        r'href="(https://www\.dafontfree\.io/download/[^"]*wpdmdl=\d+[^"]*)"', page
    )
    if m:
        return html.unescape(m.group(1))
    m = re.search(r'href="(https://www\.dafontfree\.io/download/[a-z0-9-]+/)"', page)
    if m:
        inter = _get(html.unescape(m.group(1)), referer=candidat.url)
        if inter:
            m2 = re.search(
                r'href="(https://www\.dafontfree\.io/download/[^"]*wpdmdl=\d+[^"]*)"',
                inter,
            )
            if m2:
                return html.unescape(m2.group(1))
    return None


# --------------------------------------------------------------- dafont.com

def chercher_dafont(terme: str) -> list[Candidat]:
    """Cherche sur dafont.com."""
    q = urllib.parse.quote_plus(terme)
    page = _get(f"https://www.dafont.com/fr/search.php?q={q}")
    if not page:
        return []

    cible = _normaliser(terme)
    vus, out = set(), []
    for slug in re.findall(r'href="([a-z0-9][a-z0-9-]*\.font)"', page):
        if slug in vus:
            continue
        vus.add(slug)
        nom = slug[: -len(".font")].replace("-", " ")
        out.append(
            Candidat(
                nom=nom,
                source="dafont",
                url=f"https://www.dafont.com/fr/{slug}",
                exact=_normaliser(nom) == cible,
            )
        )
        if len(out) >= 6:
            break
    return out


def _dafont_lien(candidat: Candidat) -> str | None:
    """URL dl.dafont.com/dl/?f=<slug> depuis la page d'une police."""
    page = _get(candidat.url)
    if not page:
        return None
    m = re.search(r"(//dl\.dafont\.com/dl/\?f=[a-z0-9-]+)", page)
    return f"https:{m.group(1)}" if m else None


# ------------------------------------------------------------------ commun

SOURCES: dict[str, tuple] = {
    "google": (chercher_google, None),  # telechargement direct, pas de page
    "dafontfree": (chercher_dafontfree, _dafontfree_lien),
    "dafont": (chercher_dafont, _dafont_lien),
}


def _nom_depuis_url(url: str) -> str:
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    if "filename" in q and q["filename"]:
        return q["filename"][0]
    if "f" in q and q["f"]:
        return f"{q['f'][0]}.zip"
    return "police.zip"


def extraire(archive: Path, destination: Path) -> list[Path]:
    """Deballe une archive et retourne les polices qu'elle contient."""
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(archive) as z:
            z.extractall(destination)
    except zipfile.BadZipFile:
        logger.warning("archive illisible : %s", archive.name)
        return []
    return sorted(p for p in destination.rglob("*") if p.suffix.lower() in EXTS_POLICE)


def _ecrire(data: bytes, nom: str, dossier: Path) -> tuple[Path | None, list[Path]]:
    """Ecrit un fichier telecharge (zip ou police nue)."""
    if data[:2] == b"PK":
        cible = dossier / nom
        cible.write_bytes(data)
        return cible, extraire(cible, dossier / cible.stem)
    if data[:4] in MAGICS:
        ext = ".otf" if data[:4] == b"OTTO" else ".ttf"
        cible = dossier / (Path(nom).stem + ext)
        cible.write_bytes(data)
        return cible, [cible]
    return None, []


def telecharger_candidat(candidat: Candidat, dossier: Path | None = None) -> Resultat:
    """Telecharge un candidat precis."""
    dossier = dossier or DOWNLOADS
    dossier.mkdir(parents=True, exist_ok=True)
    res = Resultat(demandee=candidat.nom, candidat=candidat)

    if candidat.source == "google":
        url = _google_url_fichier(candidat.nom)
        if not url:
            res.erreur = "fichier introuvable sur le depot Google"
            return res
        data = _get(url, binaire=True)
        if not data:
            res.erreur = "telechargement echoue"
            return res
        nom = urllib.parse.unquote(url.rsplit("/", 1)[-1])
        res.archive, res.fichiers = _ecrire(data, nom, dossier)
    else:
        _, resolveur = SOURCES[candidat.source]
        lien = resolveur(candidat) if resolveur else None
        if not lien:
            res.erreur = "lien de telechargement introuvable"
            return res
        data = _get(lien, referer=candidat.url, binaire=True)
        if not data:
            res.erreur = "telechargement echoue"
            return res
        res.archive, res.fichiers = _ecrire(data, _nom_depuis_url(lien), dossier)

    if not res.fichiers:
        res.erreur = res.erreur or "fichier recu invalide"
    return res


def telecharger(
    nom: str,
    dossier: Path | None = None,
    termes: list[str] | None = None,
    sources: tuple[str, ...] = ("google", "dafontfree", "dafont"),
) -> Resultat:
    """Cascade : vraie police -> police similaire -> rien.

    Ne leve jamais : remplit .erreur. Si rien n'est trouve, l'appelant laisse
    fichier_remplacement vide et BabelDOC utilisera sa police par defaut.
    """
    dossier = dossier or DOWNLOADS
    dossier.mkdir(parents=True, exist_ok=True)
    res = Resultat(demandee=nom)

    if termes is None:
        termes = _mots_cles(nom)

    # --- PASSE 1 : la vraie police -----------------------------------------
    for terme in termes:
        for src in sources:
            candidats = SOURCES[src][0](terme)
            exacts = [c for c in candidats if c.exact]
            if not exacts:
                continue
            logger.info("exact : %s (%s)", exacts[0].nom, src)
            r = telecharger_candidat(exacts[0], dossier)
            if r.ok:
                r.demandee, r.terme = nom, terme
                return r

    # --- PASSE 2 : une police similaire ------------------------------------
    for terme in termes:
        for src in sources:
            candidats = SOURCES[src][0](terme)
            proches = [c for c in candidats if not c.exact]
            if not proches:
                continue
            logger.info("similaire : %s (%s)", proches[0].nom, src)
            r = telecharger_candidat(proches[0], dossier)
            if r.ok:
                r.demandee, r.terme = nom, terme
                return r

    # --- PASSE 3 : rien -----------------------------------------------------
    res.erreur = "aucune police trouvee (defaut de BabelDOC)"
    return res


# ------------------------------------------------------------- mots-cles LLM

def _mots_cles_llm(police: str, famille: str, contexte: str = "") -> list[str]:
    """Demande au LLM quels termes chercher."""
    prompt = f"""Tu cherches une police pour remplacer celle utilisee dans un document.

Police utilisee : {police}
Famille : {famille}
{f"Contexte du document : {contexte}" if contexte else ""}

Donne les termes de recherche a essayer, du PLUS PRECIS au plus general.
- Le nom exact d'abord, tel qu'on le taperait sur un site de polices
- Puis le nom de famille sans le style (ex: "FuturaPT-Book" -> "futura pt")
- Puis des equivalents libres connus (ex: Futura -> Jost, Spartan)

Regles :
- 2 a 5 termes maximum
- minuscules, sans tiret ni underscore
- PAS de mots trop generiques seuls ("serif", "sans", "bold") : ils rameneraient
  tout le site
- si le nom contient un mot distinctif, garde-le (gothic, futura, garamond...)

Reponds UNIQUEMENT par un tableau JSON de chaines, sans commentaire :
["futura pt", "futura", "jost"]"""

    raw = _llm(prompt)
    termes = [t for t in _parse_json_array(raw) if isinstance(t, str)]
    termes = [t.strip().lower() for t in termes if t and t.strip()]
    generiques = {"serif", "sans", "sans serif", "bold", "italic", "regular", "font"}
    return [t for t in termes if t not in generiques][:5]


def _mots_cles(nom: str, famille: str = "", contexte: str = "") -> list[str]:
    """Termes de recherche : LLM d'abord, repli deterministe sinon."""
    try:
        termes = _mots_cles_llm(nom, famille or nom, contexte)
        if termes:
            return termes
    except Exception as e:  # noqa: BLE001
        logger.debug("mots-cles LLM indisponibles (%s)", e)

    s = re.sub(
        r"-(?:Bold|Italic|Medium|Book|Regular|Light|Thin|Black|Heavy|Oblique).*$", "", nom
    )
    espace = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)
    espace = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", espace)
    mots = espace.lower().split()
    out = []
    if len(mots) > 1:
        out.append(" ".join(mots))
    if mots:
        out.append(mots[0])
    return out


def _auto_test() -> int:
    """Verifie les 3 sources et la cascade."""
    print(f"destination : {DOWNLOADS}")

    print("\n--- 1. Google Fonts : 'jost' ---")
    for c in chercher_google("jost")[:3]:
        print(f"   {'EXACT ' if c.exact else 'proche'} {c.nom} ({c.categorie})")

    print("\n--- 2. dafontfree : 'futura' ---")
    for c in chercher_dafontfree("futura")[:3]:
        print(f"   {'EXACT ' if c.exact else 'proche'} {c.nom}")

    print("\n--- 3. dafont.com : 'futura' ---")
    for c in chercher_dafont("futura")[:3]:
        print(f"   {'EXACT ' if c.exact else 'proche'} {c.nom}")

    print("\n--- 4. cascade sur 'jost' (doit trouver l'exacte) ---")
    res = telecharger("jost")
    print(f"   type    : {res.type_trouve}")
    print(f"   source  : {res.candidat.source if res.candidat else '-'}")
    print(f"   nom     : {res.candidat.nom if res.candidat else '-'}")
    print(f"   fichiers: {len(res.fichiers)}")
    for f in res.fichiers[:3]:
        print(f"      {f.name}")
    if res.erreur:
        print(f"   erreur  : {res.erreur}")

    assert res.ok, f"cascade echouee : {res.erreur}"
    assert res.type_trouve == "exacte", "aurait du trouver la police exacte"
    print("\nOK : les 3 sources repondent, la cascade fonctionne")
    return 0


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="  %(message)s")
    sys.exit(_auto_test())
