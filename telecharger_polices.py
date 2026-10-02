#!/usr/bin/env python3
"""Recherche et telecharge des polices depuis dafontfree.io.

Chaine de telechargement (verifiee) :

    1. https://www.dafontfree.io/?s=<police>          -> pages candidates
    2. https://www.dafontfree.io/<slug>/              -> page de la police
    3. https://www.dafontfree.io/download/<slug>/     -> lien ?wpdmdl=<id>
    4. https://www.dafontfree.io/download/<slug>/?wpdmdl=<id>  -> le .zip

Tout ce qui est telecharge va dans Work/downloads/ et n'en bouge pas :
c'est a l'utilisateur (ou a un script separe) de choisir quoi installer.

Aucune ecriture dans Work/analyse/polices/ ici : ce module ne fait que
telecharger.
"""
from __future__ import annotations

import html
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

BASE = "https://www.dafontfree.io"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
TIMEOUT = 90

REPO = Path(__file__).resolve().parent
DOWNLOADS = REPO.parent / "Work" / "downloads"

# Extensions de police reconnues dans une archive.
EXTS_POLICE = (".ttf", ".otf")


@dataclass
class Resultat:
    """Ce qu'on a trouve pour une police demandee."""

    demandee: str
    page: str = ""
    archive: Path | None = None
    fichiers: list[Path] = field(default_factory=list)
    erreur: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.fichiers)


def _get(url: str, referer: str | None = None) -> str | None:
    """GET avec User-Agent. Retourne le texte, ou None si echec."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    if referer:
        req.add_header("Referer", referer)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        logger.debug("HTTP %s sur %s", e.code, url)
    except Exception as e:  # noqa: BLE001
        logger.debug("%s sur %s", type(e).__name__, url)
    return None


def _slugifier(nom: str) -> str:
    """'Futura PT' -> 'futura-pt'. Sert a construire une URL de recherche."""
    s = nom.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def _mots_cles(nom: str) -> list[str]:
    """'FuturaPT-Book' -> ['futura pt', 'futura', 'futurapt'].

    dafontfree ne trouve rien pour 'FuturaPT' colle, mais trouve 'futura'.
    On essaie du plus precis au plus general : 'serif gothic' avant 'serif',
    sinon 'SerifGothic' ramene toutes les polices serif du site.
    """
    s = nom.strip()
    # separe CamelCase : FuturaPT -> Futura PT
    espace = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)
    espace = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", espace)
    mots = espace.lower().split()

    candidats = []
    if len(mots) > 1:
        candidats.append(" ".join(mots))  # 'futura pt' - le plus precis
    if mots:
        candidats.append(mots[0])  # 'futura'
    candidats.append(s.lower())  # 'futurapt'

    # dedoublonne en gardant l'ordre
    vus, out = set(), []
    for c in candidats:
        if c and c not in vus:
            vus.add(c)
            out.append(c)
    return out


def chercher(nom: str, max_pages: int = 5) -> list[str]:
    """Pages candidates pour une police, les plus pertinentes d'abord."""
    for mot in _mots_cles(nom):
        pages = _chercher_mot(mot, max_pages)
        if pages:
            return pages
    return []


def _chercher_mot(mot: str, max_pages: int = 5) -> list[str]:
    """Une seule requete de recherche."""
    q = urllib.parse.quote_plus(mot)
    page = _get(f"{BASE}/?s={q}")
    if not page:
        return []

    # les liens de resultats ressemblent a href="https://www.dafontfree.io/<slug>/"
    trouves = re.findall(r'href="(https://www\.dafontfree\.io/[a-z0-9][a-z0-9-]*/)"', page)
    # on ecarte les pages de navigation
    exclure = {
        "feed", "contact", "about", "privacy-policy", "dmca", "disclaimer",
        "terms", "terms-of-service", "category", "tag", "author", "page",
        "wp-json", "comments", "cookie-policy", "refund-policy",
        "copyright", "faq", "blog", "news",
    }
    vus, resultat = set(), []
    for url in trouves:
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        if slug in exclure or slug in vus:
            continue
        vus.add(slug)
        resultat.append(url)
        if len(resultat) >= max_pages:
            break
    return resultat


def _lien_archive(page_html: str, page_url: str) -> str | None:
    """Extrait l'URL ?wpdmdl=<id> depuis une page de police."""
    # le bouton porte class="wpdm-download-link" ou "inddl"
    m = re.search(
        r'href="(https://www\.dafontfree\.io/download/[^"]*wpdmdl=\d+[^"]*)"',
        page_html,
    )
    if m:
        return html.unescape(m.group(1))

    # repli : un lien /download/<slug>/ puis on ira chercher le wpdmdl
    m = re.search(r'href="(https://www\.dafontfree\.io/download/[a-z0-9-]+/)"', page_html)
    if m:
        inter = _get(html.unescape(m.group(1)), referer=page_url)
        if inter:
            m2 = re.search(
                r'href="(https://www\.dafontfree\.io/download/[^"]*wpdmdl=\d+[^"]*)"',
                inter,
            )
            if m2:
                return html.unescape(m2.group(1))
    return None


def _nom_depuis_url(url: str) -> str:
    """Le ?filename=... de l'URL, sinon un nom de repli."""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    if "filename" in q and q["filename"]:
        return q["filename"][0]
    return "police.zip"


def telecharger(nom: str, dossier: Path | None = None) -> Resultat:
    """Cherche puis telecharge une police. Ne leve jamais : remplit .erreur."""
    dossier = dossier or DOWNLOADS
    dossier.mkdir(parents=True, exist_ok=True)
    res = Resultat(demandee=nom)

    pages = chercher(nom)
    if not pages:
        res.erreur = "aucun resultat de recherche"
        return res

    # on essaie les pages dans l'ordre jusqu'a obtenir une archive
    for page_url in pages:
        contenu = _get(page_url)
        if not contenu:
            continue
        lien = _lien_archive(contenu, page_url)
        if not lien:
            continue

        nom_fichier = _nom_depuis_url(lien)
        cible = dossier / nom_fichier
        if cible.exists():
            logger.info("deja telecharge : %s", cible.name)
        else:
            req = urllib.request.Request(lien, headers={"User-Agent": UA, "Referer": page_url})
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    data = r.read()
            except Exception as e:  # noqa: BLE001
                res.erreur = f"telechargement echoue ({type(e).__name__})"
                continue
            # un zip commence par PK
            if not data.startswith(b"PK"):
                continue
            cible.write_bytes(data)
            logger.info("telecharge : %s (%.0f Ko)", cible.name, len(data) / 1024)

        res.page = page_url
        res.archive = cible
        res.fichiers = extraire(cible, dossier / cible.stem)
        return res

    res.erreur = res.erreur or "aucune archive trouvee"
    return res


def extraire(archive: Path, destination: Path) -> list[Path]:
    """Deballe une archive et retourne les polices qu'elle contient."""
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(archive) as z:
            z.extractall(destination)
    except zipfile.BadZipFile:
        logger.warning("archive illisible : %s", archive.name)
        return []
    return sorted(
        p for p in destination.rglob("*") if p.suffix.lower() in EXTS_POLICE
    )


def _auto_test() -> int:
    """Telecharge une police connue et verifie qu'on obtient des fichiers."""
    print(f"destination : {DOWNLOADS}")
    res = telecharger("metrofutura")
    print(f"\ndemande    : {res.demandee}")
    print(f"page       : {res.page or '(aucune)'}")
    print(f"archive    : {res.archive.name if res.archive else '(aucune)'}")
    print(f"polices    : {len(res.fichiers)}")
    for f in res.fichiers[:8]:
        print(f"   {f.relative_to(DOWNLOADS)}")
    if res.erreur:
        print(f"erreur     : {res.erreur}")

    assert res.ok, f"echec du telechargement : {res.erreur}"
    assert all(f.suffix.lower() in EXTS_POLICE for f in res.fichiers)
    print("\nOK : telechargement et extraction fonctionnels")
    return 0


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="  %(message)s")
    sys.exit(_auto_test())
