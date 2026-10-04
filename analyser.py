#!/usr/bin/env python3
"""Passe d'analyse d'un lot de PDFs : contexte, glossaire, polices.

Tout est ecrit dans <projet>/analyse/etat.db : contexte, glossaire et
polices. Les CSV n'existent plus comme stockage — le glossaire n'est exporte
en CSV que temporairement, au moment de la traduction, parce que BabelDOC ne
sait lire que ca.

  polices/       dossier ou deposer les TTF de remplacement

Les tables many-to-many (page_glossaire, police_polices) portent les sources :
un terme n'est orphelin que si TOUS ses PDF ont disparu.

Relancer le script ne detruit RIEN : les entrees deja presentes (donc deja
corrigees a la main) sont conservees, seules les nouveautes sont ajoutees.

Usage:
  .venv/Scripts/python.exe analyser.py                     # <projet>/source/
  .venv/Scripts/python.exe analyser.py "chemin/source" -o "chemin/travail"
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor

import base as _db
import donnees as _donnees
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------- config

PROXY = os.environ.get("PDF2ZH_PROXY", "http://127.0.0.1:8645/v1")
API_KEY = os.environ.get("PDF2ZH_KEY", "hermes")
# ling-sante : le plus rapide, sort du JSON valide sans response_format
# (il ne supporte PAS response_format=json_object -> HTTP 400).
# Modele par defaut. Un projet peut en choisir un autre : options.json est
# lu a chaque run, donc l'onglet Options compte vraiment.
MODEL = os.environ.get("PDF2ZH_MODEL", "")

# Au dela, une page est decoupee en morceaux (le LLM perd le fil sur du long texte).
CHUNK_CHARS = 5000
# Sous ce nombre de caracteres, une page n'a rien a extraire.
MIN_PAGE_CHARS = 40
# space-bunny-alpha raisonne avant de repondre : sur une page dense, un
# appel peut depasser 2 minutes. 600 s evite les timeouts sur le glossaire.
TIMEOUT = int(os.environ.get("PDF2ZH_TIMEOUT", "600"))

# ---------------------------------------------------------------- LLM


def _projet_courant() -> Path:
    """Le projet : PDF2ZH_PROJET, sinon le premier trouve. None si aucun."""
    projets = Path(__file__).resolve().parent.parent / "Projets"
    nom = os.environ.get("PDF2ZH_PROJET")
    if nom:
        p = projets / nom
        if p.is_dir():
            return p
    if projets.is_dir():
        for d in sorted(projets.iterdir()):
            if d.is_dir() and not d.name.startswith("."):
                return d
    return Path(".")


def _modele_du_projet(projet: Path) -> str:
    """Le modele a utiliser : env > options.json du projet > defaut.

    Sans ca, changer le modele dans l'interface n'aurait aucun effet sur
    l'analyse : MODEL etait fige au chargement du module.
    """
    env = os.environ.get("PDF2ZH_MODEL")
    if env:
        return env
    f = projet / "options.json"
    if f.is_file():
        try:
            import json as _j

            m = (_j.loads(f.read_text(encoding="utf-8")).get("model") or "").strip()
            if m:
                return m
        except (OSError, ValueError):
            pass
    return MODELE_DEFAUT


# Les modeles gratuits epuisent leur quota (HTTP 429). space-bunny-alpha repond
# et supporte json_object : c'est lui qui sert par defaut.
MODELE_DEFAUT = "stealth/space-bunny-alpha"
MODEL = MODELE_DEFAUT

# niveaux de raisonnement, mesures avec test_effort.py :
#   low     16s  26 termes
#   medium  34s  22 termes
#   high    49s   0 termes (JSON tronque)
#   max     46s   0 termes (JSON tronque)
# Le raisonnement consomme le budget : au-dela de "medium", il ne reste plus
# de place pour le JSON et le glossaire arrive vide.
EFFORT = "low"

# le raisonnement compte dans ce budget. A 4000, "high" et "max" le consomment
# entier et renvoient un JSON coupe — d'ou 0 terme.
MAX_TOKENS = 16000

def llm(prompt: str) -> str:
    """Un appel au proxy Hermes. Retourne le texte, chaine vide si echec."""
    body = json.dumps(
        {
            "model": _modele_du_projet(_projet_courant()),
            "messages": [{"role": "user", "content": prompt}],
            # space-bunny-alpha exige une reflexion active : sans ce parametre
            # il repond 400 ("Reasoning is mandatory"). Les modeles gratuits,
            # eux, le refusent — d'ou le defaut explicite plutot que l'absence.
            #
            # effort "low" : mesure sur le meme appel, c'est 3 fois plus rapide
            # que "max" (16s contre 50s) pour 26 termes contre 0. Le raisonnement
            # mange le budget max_tokens et tronque le JSON : c'est ce qui
            # faisait perdre tous les termes a "high" et "max", pas la lenteur.
            "reasoning": {"enabled": True, "effort": EFFORT},
            "max_tokens": MAX_TOKENS,
            "temperature": 0,
        }
    ).encode()
    req = urllib.request.Request(
        PROXY + "/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            d = json.load(r)
        msg = d["choices"][0]["message"]
        # la reflexion peut deborder dans content : on ne garde que le texte
        contenu = (msg.get("content") or "").strip()
        if isinstance(msg.get("reasoning"), str):
            r = msg["reasoning"].strip()
            if r and contenu.startswith(r):
                contenu = contenu[len(r):].strip()
        return contenu
    except urllib.error.HTTPError as e:
        print(f"    ! HTTP {e.code}: {e.read().decode()[:120]}", file=sys.stderr)
    except urllib.error.URLError as e:
        raison = getattr(e, "reason", e)
        print(f"    ! {type(raison).__name__}: {str(raison)[:120]}"
              + (" — cette page sera sautee" if isinstance(raison, TimeoutError)
                 else ""), file=sys.stderr)
    except Exception as e:
        print(f"    ! {type(e).__name__}: {str(e)[:120]}", file=sys.stderr)
    return ""


def parse_json_array(raw: str):
    """Extrait un tableau JSON d'une reponse LLM (retire les ```json)."""
    if not raw:
        return []
    t = raw.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    i, j = t.find("["), t.rfind("]")
    if i == -1 or j == -1 or j < i:
        # objet seul -> on l'emballe
        i, j = t.find("{"), t.rfind("}")
        if i == -1 or j == -1:
            return []
        t = "[" + t[i : j + 1] + "]"
    else:
        t = t[i : j + 1]
    try:
        d = json.loads(t)
    except json.JSONDecodeError:
        return []
    if isinstance(d, dict):
        d = [d]
    return d if isinstance(d, list) else []


# ---------------------------------------------------------------- PDF


def pdf_pages(path: Path) -> list[str]:
    """Texte page par page. Retourne [] si le PDF est illisible."""
    try:
        import pymupdf
    except ImportError:
        import fitz as pymupdf
    try:
        doc = pymupdf.open(str(path))
    except Exception as e:
        print(f"  ! {path.name} illisible: {e}", file=sys.stderr)
        return []
    pages = []
    for p in doc:
        t = p.get_text()
        t = re.sub(r"[ \t]+", " ", t)
        t = re.sub(r"\n{2,}", "\n", t).strip()
        pages.append(t)
    doc.close()
    return pages


def pdf_fonts(path: Path) -> list[tuple[str, str]]:
    """[(famille, police)] sans le prefixe de sous-ensemble (ABCDEF+)."""
    try:
        import pymupdf
    except ImportError:
        import fitz as pymupdf
    out = set()
    try:
        doc = pymupdf.open(str(path))
    except Exception:
        return []
    for p in doc:
        for f in p.get_fonts(full=True):
            name = re.sub(r"^[A-Z]{6}\+", "", f[3] or "")
            if not name:
                continue
            fam = re.split(
                r"-(?:Bold|Italic|Medium|Book|Cn|Bd|Ex|Extra|Obl|Black|Regular|Light|Thin)",
                name,
            )[0]
            out.add((fam, name))
    doc.close()
    return sorted(out)


def pdf_fonts_usage(path: Path) -> dict[str, dict]:
    """Combien de fois chaque police est reellement utilisee.

    Parcourt les spans de chaque page : chaque span porte sa police. On
    compte les spans (une suite de caracteres d'un seul style) et les pages
    ou la police apparait. C'est une mesure d'usage, pas d'occupation.

    Retourne {nom_police: {"spans": n, "pages": n}}.
    """
    try:
        import pymupdf
    except ImportError:
        import fitz as pymupdf
    try:
        doc = pymupdf.open(str(path))
    except Exception:
        return {}

    usage: dict[str, dict] = {}
    for pno, page in enumerate(doc, 1):
        try:
            d = page.get_text("dict")
        except Exception:
            continue
        vues: set[str] = set()
        for bloc in d.get("blocks", []):
            for ligne in bloc.get("lines", []):
                for span in ligne.get("spans", []):
                    nom = re.sub(r"^[A-Z]{6}\+", "", span.get("font") or "")
                    if not nom or not (span.get("text") or "").strip():
                        continue
                    e = usage.setdefault(nom, {"spans": 0, "pages": 0})
                    e["spans"] += 1
                    vues.add(nom)
        for nom in vues:
            usage[nom]["pages"] += 1
    doc.close()
    return usage


def compter_terme(terme: str, pages: list[str]) -> tuple[int, int]:
    """(occurrences totales, nombre de pages) pour un terme dans un PDF.

    Recherche insensible a la casse, avec frontieres de mot quand le terme
    commence et finit par un caractere de mot. Un terme ponctue (« Holy
    Jambu - ») est cherche tel quel, sinon il ne matcherait jamais.
    """
    if not terme.strip() or not pages:
        return 0, 0

    debut = r"\b" if terme[0].isalnum() else ""
    fin = r"\b" if terme[-1].isalnum() else ""
    try:
        motif = re.compile(debut + re.escape(terme) + fin, re.IGNORECASE)
    except re.error:
        return 0, 0

    total = 0
    nb_pages = 0
    for txt in pages:
        n = len(motif.findall(txt))
        if n:
            total += n
            nb_pages += 1
    return total, nb_pages


def pdf_fonts_names(path: Path) -> list[str]:
    """Les noms de police d'un PDF, sans la famille. Pour lier l'usage."""
    return [nom for _fam, nom in pdf_fonts(path)]


def chunks(text: str) -> list[str]:
    """Decoupe une page trop longue aux fins de paragraphe."""
    if len(text) <= CHUNK_CHARS:
        return [text]
    out, buf = [], ""
    for para in text.split("\n"):
        if len(buf) + len(para) + 1 > CHUNK_CHARS and buf:
            out.append(buf.strip())
            buf = ""
        buf += para + "\n"
    if buf.strip():
        out.append(buf.strip())
    return out


# ---------------------------------------------------------------- etapes


def _fichier_present(fichier: str, font_dir: Path) -> bool:
    """Le fichier_remplacement pointe-t-il sur un fichier reel ?

    Accepte un nom nu (relatif a font_dir), un chemin relatif au depot, ou un
    chemin absolu. Sans extension, essaie .ttf puis .otf.
    """
    p = Path(fichier)
    candidats = [p] if p.is_absolute() else [font_dir / p, Path(__file__).resolve().parent / p]
    if p.suffix.lower() not in (".ttf", ".otf"):
        candidats += [c.with_suffix(e) for c in list(candidats) for e in (".ttf", ".otf")]
    return any(c.is_file() for c in candidats)


CONSIGNES_PROMPT = """Tu prepares la recherche de polices de remplacement pour un lot de PDFs.

L'utilisateur a ecrit des consignes. Lis-les attentivement.

=== CONSIGNES DE L'UTILISATEUR ===
{consignes}
=== FIN DES CONSIGNES ===

Polices utilisees dans les documents :
{polices}

Contexte des documents :
{contexte}

A partir des consignes, produis un plan de recherche. Reponds UNIQUEMENT par
un objet JSON, sans commentaire :

{{
  "sites": ["https://exemple.com/", ...],
  "par_police": {{
    "NomPoliceDuPdf": {{
      "chercher": ["terme1", "terme2"],
      "raison": "pourquoi ce choix, en une phrase"
    }}
  }},
  "interdits": ["terme a ne jamais utiliser"],
  "notes": "remarque generale utile pour la recherche"
}}

Regles :
- "sites" : uniquement les sites que l'utilisateur a explicitement demandes.
  Liste vide si aucun.
- "par_police" : une entree par police listee ci-dessus. Si l'utilisateur a
  impose une police precise pour un usage, mets-la en premier dans "chercher".
- "chercher" : 1 a 4 termes, minuscules, sans tiret. Du plus precis au plus
  general. PAS de mots generiques seuls (serif, sans, bold).
- "interdits" : ce que l'utilisateur refuse explicitement.
- Si les consignes ne disent rien d'utile, renvoie des listes vides et des
  "chercher" vides : le systeme fera sa recherche automatique normale.
"""


def _lire_consignes(chemin: Path) -> str:
    """Lit le fichier de consignes de l'utilisateur, s'il existe.

    On retire les lignes de commentaire pur (titres markdown, separateurs) pour
    ne pas noyer le LLM, mais on garde tout le texte ecrit.
    """
    if not chemin.is_file():
        return ""
    texte = chemin.read_text(encoding="utf-8", errors="replace")
    lignes = []
    for ligne in texte.splitlines():
        s = ligne.strip()
        # on saute les separateurs et les titres de structure du modele
        if s in ("---", "***", "___"):
            continue
        lignes.append(ligne)
    return "\n".join(lignes).strip()


def _plan_polices(consignes: str, polices: list[str], contexte: str) -> dict:
    """Demande au LLM un plan de recherche a partir des consignes.

    Retourne {"sites": [...], "par_police": {...}, "interdits": [...], "notes": str}.
    En cas d'echec, retourne un plan vide (la recherche automatique prend le relais).
    """
    vide = {"sites": [], "par_police": {}, "interdits": [], "notes": ""}
    if not consignes.strip():
        return vide

    raw = llm(
        CONSIGNES_PROMPT.format(
            consignes=consignes[:6000],
            polices="\n".join(f"- {p}" for p in polices),
            contexte=contexte[:2000] or "(aucun)",
        )
    )
    if not raw:
        return vide

    # le LLM peut renvoyer un objet ou un tableau : on veut l'objet
    t = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    t = re.sub(r"\s*```$", "", t)
    i, j = t.find("{"), t.rfind("}")
    if i == -1 or j == -1:
        return vide
    try:
        plan = json.loads(t[i : j + 1])
    except json.JSONDecodeError:
        return vide
    if not isinstance(plan, dict):
        return vide

    # normalisation
    sites = [s for s in plan.get("sites", []) if isinstance(s, str) and s.startswith("http")]
    par_police = plan.get("par_police", {})
    if not isinstance(par_police, dict):
        par_police = {}
    interdits = [s.lower() for s in plan.get("interdits", []) if isinstance(s, str)]
    return {
        "sites": sites,
        "par_police": par_police,
        "interdits": interdits,
        "notes": str(plan.get("notes", "")),
    }


def _chercher_manquantes(
    found: dict[str, dict],
    font_dir: Path,
    contexte: str = "",
    consignes: str = "",
) -> None:
    """Cherche les polices sans remplacement.

    Si l'utilisateur a ecrit des consignes, un LLM en tire d'abord un plan
    (sites prioritaires, polices imposees, interdits), qui pilote la recherche.

    N'installe RIEN dans font_dir : les archives restent dans <projet>/downloads/.
    """
    manquantes = [n for n, e in found.items() if not e.get("fichier")]
    if not manquantes:
        return

    try:
        import telecharger_polices as dl
    except ImportError:
        print("    (module de telechargement indisponible)")
        return

    # --- plan issu des consignes ------------------------------------------
    plan = _plan_polices(consignes, manquantes, contexte)
    if plan["sites"]:
        dl.ajouter_sites_prioritaires(plan["sites"])
        print(f"  consignes : {len(plan['sites'])} site(s) prioritaire(s)")
        for s in plan["sites"]:
            print(f"    {s}")
    if plan["notes"]:
        print(f"  consignes : {plan['notes'][:120]}")

    print(f"  {len(manquantes)} police(s) sans remplacement -> recherche")

    trouvees = 0
    for nom in manquantes:
        famille = found[nom]["famille"] or nom

        # les consignes priment : si l'utilisateur a impose des termes, on les
        # utilise tels quels, sinon on demande au LLM de les deduire du nom.
        impose = plan["par_police"].get(nom) or {}
        termes = [t for t in impose.get("chercher", []) if isinstance(t, str)]
        raison_consignes = ""
        if termes:
            raison_consignes = impose.get("raison", "")
            print(f"    {nom:24} termes (consignes) : {', '.join(termes)}")
            if raison_consignes:
                print(f"      {'':24} {raison_consignes[:90]}")
        else:
            termes = dl._mots_cles(nom, famille, contexte)
            print(f"    {nom:24} termes : {', '.join(termes) or '(aucun)'}")

        # on retire les termes que l'utilisateur refuse
        if plan["interdits"]:
            avant = len(termes)
            termes = [t for t in termes if not any(i in t.lower() for i in plan["interdits"])]
            if len(termes) != avant:
                print(f"      {'':24} ({avant - len(termes)} terme(s) ecarte(s) par les consignes)")

        res = dl.telecharger(nom, termes=termes)
        if res.ok:
            trouvees += 1
            fichier = res.fichiers[0] if res.fichiers else None
            found[nom]["piste"] = str(res.archive) if res.archive else ""
            found[nom]["propose"] = fichier.name if fichier else ""
            found[nom]["origine"] = "auto"
            # on garde trace du POURQUOI : consignes si l'utilisateur en a
            # donne, sinon le detail de la recherche (source, type, terme)
            if raison_consignes:
                found[nom]["raison"] = raison_consignes
            else:
                found[nom]["raison"] = (
                    f"{res.type_trouve} sur {res.candidat.source}"
                    f" via '{res.terme}'"
                    if res.candidat
                    else res.type_trouve
                )
            archive = res.archive.name if res.archive else "(archive inconnue)"
            print(f"      {'':24} -> {archive} [{res.type_trouve}] ({len(res.fichiers)} fichiers)")
            if fichier:
                print(f"      {'':24}    proposerait : {fichier.name}")
        else:
            found[nom]["raison"] = f"rien trouve ({res.erreur})"
            print(f"      {'':24} -> rien trouve ({res.erreur})")

    if trouvees:
        print(f"  {trouvees} archive(s) dans <projet>/downloads/ - a installer a la main")
        print("    pour installer : copier le .ttf voulu dans <projet>/analyse/polices/")
        print("    puis renseigner 'remplacement' dans polices.csv")


def step_fonts(
    pdfs: list[Path],
    work: Path,
    con,
    font_dir: Path,
    auto_download: bool = True,
    contexte: str = "",
) -> None:
    """Polices : aucun LLM pour la detection, instantane.

    Si auto_download, les polices sans remplacement sont cherchees sur les
    trois sources. Les archives vont dans <projet>/downloads/ ; rien n'est
    installe automatiquement dans polices/ (c'est un choix manuel).

    Tout est ecrit dans etat.db : la table police porte le choix, et
    police_polices porte l'usage par PDF (spans, pages). C'est cette table
    qui permet de savoir si une police est orpheline.
    """
    print("\n[1/3] Polices (sans LLM)")
    found: dict[str, dict] = {}
    for pdf in pdfs:
        for fam, name in pdf_fonts(pdf):
            e = found.setdefault(
                name, {"famille": fam, "pdfs": set(), "spans": 0, "pages": set()}
            )
            e["pdfs"].add(pdf.name)
        # usage reel : combien de spans, et sur combien de pages
        for nom, u in pdf_fonts_usage(pdf).items():
            e = found.setdefault(
                nom, {"famille": nom, "pdfs": set(), "spans": 0, "pages": set()}
            )
            e["spans"] += u["spans"]
            e["pages"].add(f"{pdf.name}:{u['pages']}")
    print(f"  {len(found)} polices distinctes, {len({v['famille'] for v in found.values()})} familles")

    # existant : on ne remplace jamais un fichier deja choisi a la main
    existing = {
        r["police_origine"]: dict(r) for r in con.execute("SELECT * FROM police")
    }
    if existing:
        print(f"  {len(existing)} entrees deja presentes (conservees)")

    # un TTF du bon nom deja depose dans polices/ -> pre-remplissage
    prefilled = 0
    for name, e in found.items():
        ancien = existing.get(name, {})
        ancien_fichier = (ancien.get("remplacement") or "").strip()
        ancienne_origine = (ancien.get("origine") or "").strip()

        # 1. un fichier deja choisi ET present : on le garde tel quel
        if ancien_fichier and _fichier_present(ancien_fichier, font_dir):
            e["fichier"] = ancien_fichier
            e["origine"] = ancienne_origine or "manuel"
        # 2. un fichier choisi mais absent : on le signale et on cherche
        elif ancien_fichier:
            e["fichier"] = ""
            e["origine"] = "defaut"
            e["absent"] = ancien_fichier
        # 3. un TTF du bon nom dans polices/ -> pre-remplissage
        elif (font_dir / f"{name}.ttf").exists():
            e["fichier"] = f"{name}.ttf"
            e["origine"] = "manuel"
            prefilled += 1
        elif (font_dir / f"{name}.otf").exists():
            e["fichier"] = f"{name}.otf"
            e["origine"] = "manuel"
            prefilled += 1
        else:
            e["fichier"] = ""
            e["origine"] = "defaut"
    if prefilled:
        print(f"  {prefilled} pre-remplies depuis polices/")

    # recherche automatique des polices non remplacees
    if auto_download:
        consignes = _lire_consignes(work / "consignes-polices.md")
        if consignes:
            print("  consignes-polices.md lu")
        _chercher_manquantes(found, font_dir, contexte, consignes)

    # --- fusion : existant d'abord, puis les nouvelles polices -------------
    # `origine` : auto = propose par le systeme, manuel = choisi par
    # l'utilisateur, defaut = rien trouve (BabelDOC prend sa police).
    #
    # `remplacement` = ce qui est REELLEMENT installe (fichier present dans
    # polices/). `propose` = ce que la recherche a trouve, a copier depuis
    # <projet>/downloads/ si on le veut.
    # Ecriture : la table police porte le choix, police_polices porte l'usage.
    # Une police sans ligne dans police_polices est orpheline — c'est une
    # requete, pas un drapeau a maintenir.
    n_auto = n_man = n_def = 0
    usages: dict[str, list[tuple[Path, dict]]] = {}
    for pdf in pdfs:
        vus = pdf_fonts_usage(pdf)
        for nom in pdf_fonts_names(pdf):
            if nom in vus:
                usages.setdefault(nom, []).append((pdf, vus[nom]))

    for name, e in sorted(found.items()):
        ancien = existing.get(name, {})
        remplacement = (ancien.get("remplacement") or "").strip()
        origine = (ancien.get("origine") or "").strip() or "defaut"
        if remplacement and _fichier_present(remplacement, font_dir):
            origine = "manuel"

        pid = _donnees.upsert_police(
            con, name, remplacement, origine,
            e.get("propose", ""), e.get("raison", ""), e["famille"],
        )
        n_auto += origine == "auto"
        n_man += origine == "manuel"
        n_def += origine == "defaut"

        for pdf, u in usages.get(name, []):
            _donnees.lier_police(con, pid, _pdf_id(con, pdf.name),
                                 u["spans"], u["pages"])

    added = sum(1 for n in found if n not in existing)
    print(f"  +{added} nouvelles sur {len(found)} polices "
          f"({n_auto} auto, {n_man} manuel, {n_def} defaut)")


def _resumer_pdfs(noms) -> str:
    """Resume la liste des PDF en '3 pdfs' si elle est longue.

    La colonne est informative : la garder courte rend le CSV lisible a l'oeil.
    Le detail complet reste dans le .json du PDF.
    """
    noms = sorted(noms)
    if len(noms) <= 2:
        return " | ".join(noms)
    return f"{len(noms)} pdfs"


CTX_PAGE_PROMPT = """Tu analyses un document pour preparer sa traduction.

=== CONSIGNES DE L'UTILISATEUR ===
{consignes}
=== FIN DES CONSIGNES ===

Contexte du LOT (tous les documents) :
{lot_ctx}

Contexte de CE document ({doc}) :
{doc_ctx}

Voici le texte de la page {page}/{total} de "{doc}" :

---
{text}
---

Reponds en francais, en 4 lignes maximum, format exact :
LOT: <information valable pour TOUT LE LOT : univers commun, campagne, serie,
ton general. Seulement si cette page apporte du VRAIMENT nouveau. Si le
contexte du lot dit deja la meme chose, ecris RIEN>
DOC: <information propre a CE document : son sujet, ses personnages, sa
terminologie. Seulement si nouveau. Sinon ecris RIEN>
PAGE: <resume en une phrase de ce que contient cette page>

IMPORTANT :
- Ecris TOUT en francais, meme si le document est en anglais.
- Ne repete pas ce qui est deja dans les contextes ci-dessus. Une ligne de
  LOT ou DOC ne doit etre ajoutee que si elle apporte un fait nouveau, pas
  une reformulation de ce qui precede.
- Les consignes de l'utilisateur priment : s'il a donne une traduction
  officielle pour un terme, utilise-la. S'il a explique l'univers, appuie-toi
  dessus au lieu de le re-deduire.
"""

CTX_EMPTY = "(page sans contenu textuel exploitable)"


def prog(fait: int, total: int, libelle: str = "") -> None:
    """Emet une ligne de progression lisible par l'interface.

    Convention : [PROGRESS] fait/total  libelle
    Le serveur la lit dans le log pour animer la barre. Inoffensive en ligne
    de commande : c'est une ligne de plus dans la sortie.
    """
    print(f"[PROGRESS] {fait}/{total}" + (f"  {libelle}" if libelle else ""), flush=True)


def _lire_ligne(raw: str, prefixe: str) -> str:
    """Extrait 'PREFIXE: valeur' d'une reponse LLM. '' si absent ou RIEN."""
    for line in raw.splitlines():
        if line.startswith(prefixe):
            v = line[len(prefixe) :].strip()
            if v and v.upper() not in ("RIEN", "NONE", "-", "(rien)"):
                return v
    return ""


# ---------------------------------------------------------------- base
#
# Le contexte, le glossaire et les polices vivent dans etat.db. Les CSV ne
# survivent que comme export temporaire pour BabelDOC, qui ne sait lire que
# ca. Aucun fichier n'est donc ecrit ici, hormis les .ttf.


def _import_registre(con, projet: Path) -> None:
    """Aligne le registre sur le disque, puis ne garde que les PDF a traiter.

    Un PDF marque 'ignore' par l'utilisateur est saute, mais garde ses
    donnees : c'est tout l'interet du registre.
    """
    import registre

    r = registre.synchroniser(con, projet)
    if r["nouveaux"] or r["absents"]:
        print(f"  registre : {len(r['nouveaux'])} nouveau(x), "
              f"{len(r['absents'])} absent(s)")


def _pdf_id(con, nom_pdf: str) -> int:
    """L'id du PDF dans la base, enregistre au passage s'il est nouveau.

    On passe par le nom, ou par un nom que le PDF a porte : c'est ce qui fait
    qu'un PDF renomme retrouve son contexte.
    """
    row = _db.pdf_par_nom(con, nom_pdf)
    if row is not None:
        if row["etat"] != "present":
            _db.mettre_pdf_etat(con, row["id"], "present")
        return int(row["id"])
    return _db.enregistrer_pdf(con, nom_pdf, "")


def _ecrire_contexte(con, pdf_id: int, resume: str, points: list[str],
                     pages: dict[str, str]) -> None:
    con.execute("DELETE FROM page_contexte WHERE contexte_id IN"
                " (SELECT id FROM contexte WHERE pdf_id = ?)", (pdf_id,))
    row = con.execute("SELECT id FROM contexte WHERE pdf_id = ?", (pdf_id,)).fetchone()
    if row is None:
        cur = con.execute(
            "INSERT INTO contexte (pdf_id, resume, points, termes_cles, maj)"
            " VALUES (?, ?, ?, '[]', ?)",
            (pdf_id, resume, _json(points), _db.maintenant()),
        )
        cid = int(cur.lastrowid)
    else:
        cid = int(row["id"])
        con.execute(
            "UPDATE contexte SET resume = ?, points = ?, maj = ? WHERE id = ?",
            (resume, _json(points), _db.maintenant(), cid),
        )
    for numero, texte in sorted(pages.items(), key=lambda kv: int(kv[0])):
        con.execute(
            "INSERT OR REPLACE INTO page_contexte (contexte_id, numero, resume)"
            " VALUES (?, ?, ?)", (cid, int(numero), texte),
        )


def _lire_contexte(con, pdf_id: int) -> dict | None:
    row = con.execute("SELECT * FROM contexte WHERE pdf_id = ?", (pdf_id,)).fetchone()
    if row is None:
        return None
    pages = {
        str(x["numero"]): (x["resume"] or "")
        for x in con.execute(
            "SELECT numero, resume FROM page_contexte"
            " WHERE contexte_id = ? ORDER BY numero", (row["id"],))
    }
    return {
        "fichier": "",
        "resume": row["resume"] or "",
        "points": _json_lire(row["points"]),
        "termes_cles": _json_lire(row["termes_cles"]),
        "pages": pages,
    }


def _ecrire_lot(con, points: list[str]) -> None:
    row = con.execute("SELECT id FROM lot LIMIT 1").fetchone()
    if row is None:
        con.execute("INSERT INTO lot (points, maj) VALUES (?, ?)",
                    (_json(points), _db.maintenant()))
    else:
        con.execute("UPDATE lot SET points = ?, maj = ? WHERE id = ?",
                    (_json(points), _db.maintenant(), row["id"]))


def _lire_lot(con) -> list[str]:
    row = con.execute("SELECT points FROM lot LIMIT 1").fetchone()
    return _json_lire(row["points"]) if row else []


def _json(valeur) -> str:
    import json as _j

    return _j.dumps(valeur, ensure_ascii=False)


def _json_lire(texte: str | None) -> list:
    import json as _j

    if not texte:
        return []
    try:
        v = _j.loads(texte)
        return v if isinstance(v, list) else []
    except ValueError:
        return []


def _resume_doc(c: dict) -> str:
    """Rend un contexte JSON lisible par le LLM (et par un humain)."""
    out = []
    if c.get("resume"):
        out.append(f"Resume : {c['resume']}")
    if c.get("points"):
        out.append("Points cles :")
        out += [f"- {x}" for x in c["points"]]
    if c.get("termes_cles"):
        out.append("Termes cles : " + ", ".join(c["termes_cles"]))
    return "\n".join(out)


def _resume_lot(ctxs: list[dict]) -> str:
    """Vue condensee de tout le lot, pour nourrir les etapes suivantes."""
    out = []
    for c in ctxs:
        if c.get("resume"):
            out.append(f"- {c.get('fichier', '?')} : {c['resume']}")
    return "\n".join(out)


def step_context(pdfs: list[Path], work: Path, con) -> list[dict]:
    """Passe 1 : contexte a TROIS niveaux, un fichier JSON par PDF.

    - lot  : commun a tous les documents, enrichi en traversant les PDFs
    - doc  : resume du document
    - page : resume de chaque page

    Le contexte est ecrit dans etat.db, rattache au PDF par son id. Un
    contexte deja present est REUTILISE tel quel : le supprimer en base
    pour le recalculer.

    Retourne la liste des contextes (pour le glossaire et les polices).
    """
    print("\n[2/3] Contexte (passe 1, LLM) - 3 niveaux : lot / document / page")

    consignes = _lire_consignes(work / "consignes-contexte.md")
    if consignes:
        print("  consignes-contexte.md lu")

    # lot persistant : il s'enrichit d'un run a l'autre
    lot_lines: list[str] = _lire_lot(con)
    if lot_lines:
        print(f"  lot existant : {len(lot_lines)} points (conserves)")

    total_pages = sum(len(pdf_pages(p)) for p in pdfs)
    fait = 0
    ctxs: list[dict] = []

    for pdf in pdfs:
        pid = _pdf_id(con, pdf.name)
        deja = _lire_contexte(con, pid)
        if deja:
            nb = len(deja.get("pages", {}))
            print(f"  {pdf.name} : contexte deja present ({nb} pages) -> reutilise")
            ctxs.append({**deja, "fichier": pdf.name})
            fait += nb
            prog(fait, total_pages, pdf.name)
            continue

        pages = pdf_pages(pdf)
        doc_lines: list[str] = []
        pages_resume: dict[str, str] = {}
        print(f"  {pdf.name}: {len(pages)} pages")

        for n, txt in enumerate(pages, 1):
            fait += 1
            prog(fait, total_pages, f"{pdf.name} p{n}")
            if len(txt) < MIN_PAGE_CHARS:
                pages_resume[str(n)] = CTX_EMPTY
                continue

            raw = llm(
                CTX_PAGE_PROMPT.format(
                    consignes=consignes[:6000] or "(aucune consigne fournie)",
                    lot_ctx="\n".join(lot_lines) or "(aucun pour l'instant)",
                    doc_ctx="\n".join(doc_lines) or "(aucun pour l'instant)",
                    page=n,
                    total=len(pages),
                    doc=pdf.name,
                    text=txt[:8000],
                )
            )
            lot = _lire_ligne(raw, "LOT:")
            doc = _lire_ligne(raw, "DOC:")
            page = _lire_ligne(raw, "PAGE:")

            marques = []
            if lot:
                lot_lines.append(f"- {lot}")
                marques.append("lot")
            if doc:
                doc_lines.append(f"- {doc}")
                marques.append("doc")
            if marques:
                print(f"    p{n}: +{' +'.join(marques)}")

            pages_resume[str(n)] = page or "(vide)"

        # le fichier de ce PDF : c'est lui qu'on edite a la main
        # resume = le point principal, points = les points additionnels.
        # Evite d'avoir deux fois la meme phrase dans le fichier.
        ctx = {
            "fichier": pdf.name,
            "resume": doc_lines[0][2:] if doc_lines else "",
            "points": [l[2:] for l in doc_lines[1:]],
            "termes_cles": [],
            "pages": pages_resume,
        }
        _ecrire_contexte(con, pid, ctx["resume"], ctx["points"], pages_resume)
        print(f"    -> {pdf.name} : {len(pages_resume)} pages")
        ctxs.append({**ctx, "fichier": pdf.name})

    # meme convention que les fichiers de PDF : sans le prefixe "- "
    _ecrire_lot(con, [l[2:] if l.startswith("- ") else l for l in lot_lines])
    print(f"  -> lot : {len(lot_lines)} points")
    return ctxs


GLOSS_PROMPT = """Tu extrais les termes a traduire de facon consistante vers le {lang_out}.

=== CONSIGNES DE L'UTILISATEUR ===
{consignes}
=== FIN DES CONSIGNES ===

Contexte du document :
{global_ctx}

Resume de la page :
{page_ctx}

Termes deja retenus (ne pas les redonner) :
{known}

Texte :
---
{text}
---

Regles :
- Noms propres (personnes, lieux, organisations, objets nommes, titres) et termes
  techniques/metier. PAS de phrases, PAS de mots grammaticaux.
- Groupes nominaux courts (5 mots max).
- Traduis chaque terme vers le {lang_out}.
- IMPORTANT : les noms de salles/sections en MAJUSCULES (BARRACKS, ARMORY,
  CONTAINMENT, COFFIN TESTING...) sont du vocabulaire commun -> ILS SE TRADUISENT
  (baraquements, armurerie, confinement, essais de cercueil). Ne les garde PAS tels quels.
- Meme valeur en src et tgt UNIQUEMENT pour un vrai nom propre conserve
  (personne, marque, univers : BAKTO, Cloud Empress, Themis, Nazhun) ou un code.
  En cas de doute, TRADUIS.
- Ignore les fragments coupes, les numeros de page, les dates isolees,
  et tout terme entre chevrons comme <Rien>.
- Les consignes de l'utilisateur PRIMENT : s'il a donne une traduction imposee
  pour un terme, reprends-la EXACTEMENT. S'il a dit qu'un terme ne se traduit
  jamais, mets la meme valeur en src et tgt.

Reponds UNIQUEMENT par un tableau JSON, sans commentaire :
[{{"src": "terme", "tgt": "traduction"}}]
"""


def step_glossary(
    pdfs: list[Path], work: Path, con, ctxs: list[dict], lang_out: str
) -> None:
    """Extraction par sous-agents paralleles, puis fusion.

    Les agents sont aveugles et ne se voient pas entre eux : ils se recouvrent,
    c'est voulu. Un agent de fusion tranche ensuite avec les consignes et le
    glossaire deja valide. C'est plus rapide qu'une passe sequentielle, et
    l'isolement evite qu'une page biaise les suivantes.
    """
    print("\n[3/3] Glossaire (sous-agents en parallele + fusion)")
    import agents

    consignes = _lire_consignes(work / "consignes-glossaire.md") or _lire_consignes(
        work / "consignes-contexte.md"
    )
    if consignes:
        print("  consignes lues")

    # --- ce qui est deja valide : jamais ecrase
    # Les termes deja valides sont conserves tels quels — SAUF ceux qui ne
    # peuvent pas servir : un mot ecrit lettre par lettre sur 23 lignes, ou une
    # phrase complete. Ils ont ete extrait par une version anterieure, avant
    # le nettoyage ; on les retire une fois, puis ils ne reviennent pas.
    salies = {r["source"] for r in con.execute("SELECT * FROM glossaire")}
    tout_pages = "\n".join(t for _, _, t in
                            [(p.name, n, t) for p in pdfs
                             for n, t in enumerate(pdf_pages(p), 1)])
    a_vider = agents._nettoyer([{"src": s} for s in salies], tout_pages)
    retenus = {t["src"] for t in a_vider}
    a_purger = salies - retenus
    for terme in a_purger:
        con.execute("DELETE FROM glossaire WHERE source = ?", (terme,))
        # les liens page_glossaire suivent : sans ca, un terme purge
        # laisserait des lignes orphelines pointant sur rien
        con.execute("DELETE FROM page_glossaire WHERE glossaire_id NOT IN "
                    "(SELECT id FROM glossaire)")
    if a_purger:
        print(f"  {len(a_purger)} terme(s) pollue(s) purge(s) de la version anterieure")

    existing = {r["source"]: dict(r) for r in con.execute("SELECT * FROM glossaire")}
    if existing:
        print(f"  {len(existing)} termes deja presents (conserves tels quels)")

    # --- le plan : une tache par bloc, une page longue donne plusieurs blocs
    pages = [(pdf.name, n, t) for pdf in pdfs for n, t in enumerate(pdf_pages(pdf), 1)]
    taches = agents.planifier(pages)
    print(f"  {len(pages)} page(s) -> {len(taches)} bloc(s) "
          f"pour {agents.AGENTS_PARALLELES} agents")

    lot = _resume_lot(ctxs)
    resumes = {c.get("fichier"): c for c in ctxs if c.get("fichier")}

    # tout le texte du projet, pour verifier qu'un terme existe vraiment.
    # On ne lit PAS les termes deja valides : ils sont dans `existing`, avec
    # leur propre source, et n'ont pas besoin d'etre revérifiés.
    tout_texte = "\n".join(t for _, _, t in pages)

    def _une_tache(t: dict) -> tuple[str, int, list[dict]]:
        ctx_pdf = resumes.get(t["pdf"], {})
        prompt = agents.prompt_extraction(
            t["texte"],
            consignes=consignes,
            lot=lot,
            document=ctx_pdf.get("resume", ""),
            page=(ctx_pdf.get("pages", {}) or {}).get(str(t["page"]), ""),
            ctx=_options_agents(con),
        )
        return t["pdf"], t["page"], agents.lire_reponse(llm(prompt))

    # --- les agents, en parallele
    propositions: list[dict] = []
    par_page: dict[tuple[str, int], list[dict]] = {}
    fait = 0
    for lot_taches in _par_blots(taches, agents.AGENTS_PARALLELES):
        with ThreadPoolExecutor(max_workers=agents.AGENTS_PARALLELES) as ex:
            for nom_pdf, numero, termes in ex.map(_une_tache, lot_taches):
                if termes:
                    par_page.setdefault((nom_pdf, numero), []).extend(termes)
                    propositions.extend(termes)
                fait += 1
        prog(fait, len(taches), "extraction")
        if propositions:
            print(f"    {fait}/{len(taches)} blocs — {len(propositions)} propositions")

    if not propositions:
        print("  aucune proposition — le glossaire reste inchange")
        return

    # --- la fusion : un agent voit tous les doublons d'un coup
    print(f"  fusion de {len(propositions)} propositions...")
    prompt = agents.prompt_fusion(
        propositions,
        existant=[{"src": k, "tgt": v["target"]} for k, v in existing.items()],
        consignes=consignes,
    )
    fusionnes = agents.lire_reponse(llm(prompt))
    if not fusionnes:
        # l'agent de fusion a echoue : on garde les propositions brutes plutot
        # que de perdre le travail des sous-agents
        fusionnes = propositions
        print("    fusion sans reponse — on garde les propositions brutes")

    # --- la casse, deterministement
    # Le prompt de fusion le demande, mais un modele peut l'ignorer. Mesure
    # sur un vrai glossaire : "Refrain" et "refrain" etaient deux entrees, et
    # le mot etait remplace deux fois dans le meme paragraphe.
    avant = len(fusionnes)
    # le nettoyage vient AVANT la casse : un terme ecrit lettre par lettre sur
    # 23 lignes n'est pas une variante, c'est un artefact
    fusionnes = agents._nettoyer(fusionnes, tout_texte)
    if len(fusionnes) != avant:
        print(f"    {avant - len(fusionnes)} terme(s) pollue(s) ecarte(s)")
    avant = len(fusionnes)
    fusionnes = agents._fusionner_casse(fusionnes)
    if len(fusionnes) != avant:
        print(f"    {avant - len(fusionnes)} variante(s) de casse fusionnee(s)")

    # --- ecriture
    nb = 0
    for terme in fusionnes:
        src_t = (terme.get("src") or "").strip()
        tgt_t = (terme.get("tgt") or "").strip()
        if not src_t or not tgt_t:
            continue
        # Un terme deja en base ne devient PAS "manuel" pour autant : ca
        # protegeait 33 termes a la premiere analyse, dont aucun n'avait ete
        # vu par toi. Seul ton passage en manuel doit proteger un terme — donc
        # on conserve l'origine existante, et on laisse `upsert_terme` la
        # changer si la cible differe (c'est toi qui as corrige).
        gid = _donnees.upsert_terme(
            con, src_t, tgt_t, lang_out,
            existing.get(src_t, {}).get("origine", "auto"),
        )
        if terme.get("definition"):
            con.execute("UPDATE glossaire SET definition = ? WHERE id = ?",
                        (terme["definition"][:400], gid))
        # les pages : d'ou le terme a ete vu, par tous les agents
        for (nom_pdf, numero), termes in par_page.items():
            if any(x.get("src") == src_t for x in termes):
                _donnees.lier_pages_terme(con, gid, _pdf_id(con, nom_pdf), [numero])
        nb += 1

    # le comptage d'occurrences, sur tous les PDF du projet
    _compter_occurrences(con)
    total = con.execute("SELECT COUNT(*) FROM glossaire").fetchone()[0]
    print(f"  {nb} terme(s) fusionne(s) -> {total} au total")


def _options_agents(con) -> dict:
    """Ce que les sous-agents doivent voir, d'apres les options du projet."""
    import agents as _a

    f = Path(__file__).resolve().parent.parent / "Projets"
    return dict(_a.DEFAUTS)


def _par_blots(items: list, n: int):
    """Decoupe une liste en paquets de n, pour borner la memoire des threads."""
    for i in range(0, len(items), n):
        yield items[i:i + n]


def _tous_pdfs_du_projet(con) -> list[Path]:
    """Tous les PDF connus du projet, presents ou absents.

    Le comptage doit porter sur le PROJET, pas sur la selection du jour : un
    terme qui revient dans trois PDF du livre est bien plus important qu'un
    terme a vingt occurrences dans un seul. Compter sur la selection faisait
    chuter la note de portee a chaque analyse partielle — c'est-a-dire
    exactement quand on ne regarde qu'un extrait.
    """
    dossier = base.projet()
    if dossier is None:
        return []
    source = dossier / "source"
    connus = {r["nom"] for r in con.execute("SELECT nom FROM pdf")}
    presents = [p for p in sorted(source.glob("*.pdf")) if p.name in connus]
    # un PDF absent du disque garde sa place : ses occurrences sont celles
    # connues, pas zero
    return presents


def _compter_occurrences(con, pdfs: list[Path] | None = None) -> None:
    """Recompte occurrences et pages de chaque terme, sur tous les PDF.

    `pdfs` force une liste ; par defaut on prend tout le projet. Chaque terme
    garde aussi le nombre de PDF ou il apparait, c'est ce que la note de
    qualite utilise.
    """
    if pdfs is None:
        pdfs = _tous_pdfs_du_projet(con)
    textes: dict[str, list[str]] = {}
    for pdf in pdfs:
        if pdf.name not in textes:
            textes[pdf.name] = pdf_pages(pdf)

    nb_pdf_projet = len(textes)
    for r in con.execute("SELECT id, source FROM glossaire").fetchall():
        total = pages_vues = 0
        vus = 0
        for pages in textes.values():
            n, np_ = compter_terme(r["source"], pages)
            total += n
            pages_vues += np_
            if n:
                vus += 1
        con.execute(
            "UPDATE glossaire SET occurrences = ?, nb_pages = ?,"
            " nb_pdfs = ?, portee_totale = ? WHERE id = ?",
            (total, pages_vues, vus, nb_pdf_projet, r["id"]),
        )


MODELE_CONSIGNES_CONTEXTE = """# Consignes pour le contexte

Ce fichier est a toi. Ecris-le en francais, en langage naturel : un LLM le lit
avant d'analyser les documents, et s'en sert pour ecrire un contexte juste.

Il ne concerne QUE le contexte (pas la recherche des polices).

## Explique ce que tu sais deja

Tout ce que tu sais et que le LLM ne peut pas deviner en lisant les PDFs.

```
Cloud Empress est un jeu de role de science-fantasy post-apocalyptique.
Le ton est sombre mais pas desespere, avec une pointe d'absurde.
```

## Donne des liens vers des sources utiles

Wikipedia, site officiel, page de licence, wiki de fans.

```
Sources :
- https://cloudempress.com/
- https://en.wikipedia.org/wiki/Cloud_Empress
```

## Preciser la terminologie

```
- "Farmerling" se traduit par "Farmerling" (on garde le mot anglais).
- "Lowland Wastes" se traduit par "Terres Basses".
- "Cloud Empress" ne se traduit jamais.
```

## Notes libres

_(tout ce qui peut aider a comprendre les documents)_
"""

MODELE_CONSIGNES_POLICES = """# Consignes pour les polices

Ce fichier est a toi. Ecris-le en francais, en langage naturel : un LLM le lit
avant de lancer la recherche automatique, et s'en sert pour mieux choisir.

Il ne concerne QUE les polices (pas le contexte des documents).

## Sites a utiliser en priorite

```
Sites prioritaires :
- https://fonts.google.com/
- https://www.freefonts.io/
```

## Imposer une police pour un usage precis

```
- Les textes normaux doivent etre en Helvetica.
- Les titres doivent etre en Futura.
```

## Ecarter une police

```
- Ne pas utiliser Comic Sans, jamais.
```

## Notes libres

_(tout ce qui peut aider : contraintes, preferences, remarques)_
"""


def _creer_consignes(analyse_dir: Path) -> None:
    """Cree les fichiers de consignes s'ils manquent. N'ecrase jamais."""
    analyse_dir.mkdir(parents=True, exist_ok=True)
    for nom, modele in (
        ("consignes-contexte.md", MODELE_CONSIGNES_CONTEXTE),
        ("consignes-polices.md", MODELE_CONSIGNES_POLICES),
    ):
        cible = analyse_dir / nom
        if not cible.exists():
            cible.write_text(modele, encoding="utf-8")
            print(f"  cree : {nom}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Passe d'analyse d'un lot de PDFs",
        epilog=(
            "Les etapes sont independantes : on peut lancer le contexte sans les "
            "polices, ou les polices sans le contexte. Par defaut les trois."
        ),
    )
    ap.add_argument("source", nargs="?", default=None, help="dossier des PDFs (defaut: <projet>/source)")
    ap.add_argument("-o", "--output", default=None, help="dossier de travail (defaut: <projet>/analyse)")
    ap.add_argument("-p", "--projet", default=None, help="nom du projet (defaut: le premier trouve)")
    ap.add_argument("--creer", action="store_true", help="creer la structure du projet si absente")
    ap.add_argument("-lo", "--lang-out", default="fr")

    # --- selection des etapes (independantes) ------------------------------
    ap.add_argument(
        "--etapes",
        default="contexte,polices,glossaire",
        help=(
            "etapes a executer, separees par des virgules. "
            "Valeurs : contexte, polices, glossaire. "
            "Ex: --etapes polices   (polices seules)"
        ),
    )
    ap.add_argument("--no-download", action="store_true", help="ne pas chercher les polices sur le web")
    ap.add_argument(
        "--pdfs",
        default=None,
        help=(
            "limiter aux PDFs dont le nom contient un de ces fragments "
            "(separes par des virgules). Defaut : tous."
        ),
    )
    ap.add_argument(
        "--ignorer-contexte",
        action="store_true",
        help="recalculer le contexte meme si un fichier existe deja",
    )
    a = ap.parse_args()

    etapes = {e.strip().lower() for e in a.etapes.split(",") if e.strip()}
    inconnues = etapes - {"contexte", "polices", "glossaire"}
    if inconnues:
        print(f"etapes inconnues : {', '.join(sorted(inconnues))}", file=sys.stderr)
        print("valeurs valides : contexte, polices, glossaire", file=sys.stderr)
        return 2
    if not etapes:
        print("aucune etape demandee", file=sys.stderr)
        return 2

    # Les donnees vivent HORS du depot, un dossier par projet :
    #   ../Projets/<nom>/{source,traduits,downloads,analyse}
    # Le projet se choisit par --projet, sinon le premier trouve.
    repo = Path(__file__).resolve().parent
    projets_dir = repo.parent / "Projets"

    if a.projet:
        projet = projets_dir / a.projet
        # avec --creer, un projet absent est cree ; sinon c'est une erreur
        if not projet.is_dir() and not a.creer:
            print(f"projet introuvable : {a.projet}", file=sys.stderr)
            dispo = sorted(p.name for p in projets_dir.iterdir() if p.is_dir()) if projets_dir.is_dir() else []
            print(f"disponibles : {', '.join(dispo) or '(aucun)'}", file=sys.stderr)
            return 2
    else:
        trouves = sorted(p for p in projets_dir.iterdir() if p.is_dir()) if projets_dir.is_dir() else []
        if not trouves:
            print(f"aucun projet dans {projets_dir}", file=sys.stderr)
            print("cree-en un :  --projet <nom> --creer", file=sys.stderr)
            return 1
        projet = trouves[0]

    # creation a la volee : structure complete, rien d'ecrase
    if a.creer:
        for d in ("source", "traduits", "downloads", "analyse", "analyse/polices"):
            (projet / d).mkdir(parents=True, exist_ok=True)
        _creer_consignes(projet / "analyse")
        print(f"projet pret : {projet}")

    src = Path(a.source) if a.source else projet / "source"
    work = Path(a.output) if a.output else projet / "analyse"
    pdfs = sorted(src.glob("*.pdf"))
    if a.pdfs:
        voulus = [m.strip() for m in a.pdfs.split(",") if m.strip()]
        pdfs = [p for p in pdfs if any(v.lower() in p.name.lower() for v in voulus)]
        print(f"selection : {len(pdfs)} PDF(s) sur {len(voulus)} motif(s)")
    if not pdfs:
        print(f"aucun PDF dans {src}", file=sys.stderr)
        print("depose tes PDFs dans source/ puis relance", file=sys.stderr)
        return 1

    # Le proxy est necessaire pour les etapes qui appellent le LLM, mais on ne
    # BLOQUE jamais : on avertit et on laisse tourner. Une etape sans LLM doit
    # pouvoir s'executer, et l'utilisateur doit voir l'echec reel plutot qu'un
    # refus preventif.
    besoin_llm = bool(etapes & {"contexte", "glossaire"}) or (
        "polices" in etapes and not a.no_download
    )
    if besoin_llm:
        try:
            urllib.request.urlopen(PROXY + "/models", timeout=10)
        except Exception:
            print(f"AVERTISSEMENT: proxy Hermes injoignable sur {PROXY}", file=sys.stderr)
            print(
                "  les etapes utilisant le LLM vont echouer. "
                "Lance: hermes proxy start --provider nous --port 8645",
                file=sys.stderr,
            )

    work.mkdir(parents=True, exist_ok=True)
    font_dir = work / "polices"
    font_dir.mkdir(exist_ok=True)
    print(f"lot: {len(pdfs)} PDF(s) -> {work}")
    print(f"etapes : {', '.join(sorted(etapes))}")

    # Le contexte nourrit les polices ET le glossaire : on le calcule d'abord
    # si l'etape est demandee. Sinon on relit les fichiers JSON existants.
    # Une seule connexion pour tout le run : le contexte, les polices et le
    # glossaire partagent les memes donnees.
    with _db.connecter(projet) as con:
        _import_registre(con, projet)

        if a.ignorer_contexte:
            con.execute("DELETE FROM page_contexte")
            con.execute("DELETE FROM contexte")
            print("  contextes effaces (--ignorer-contexte)")

        if "contexte" in etapes:
            ctxs = step_context(pdfs, work, con)
        else:
            ctxs = []
            for pdf in pdfs:
                c = _lire_contexte(con, _pdf_id(con, pdf.name))
                if c:
                    ctxs.append({**c, "fichier": pdf.name})
            if ctxs:
                print(f"  {len(ctxs)} contexte(s) PDF relu(s)")


        if "polices" in etapes:
            step_fonts(
                pdfs,
                work,
                con,
                font_dir,
                auto_download=not a.no_download,
                contexte=_resume_lot(ctxs),
            )
        if "glossaire" in etapes:
            step_glossary(pdfs, work, con, ctxs, a.lang_out)

        print("\nTermine. Tout est dans etat.db. A verifier/corriger dans l'interface :")
        print(f"  contexte  {con.execute('SELECT COUNT(*) FROM contexte').fetchone()[0]} document(s)")
        print(f"  glossaire {con.execute('SELECT COUNT(*) FROM glossaire').fetchone()[0]} terme(s)")
        print(f"  polices   {con.execute('SELECT COUNT(*) FROM police').fetchone()[0]} police(s)")
        print(f"  {work/'polices'}  (depose tes .ttf ici)")

        print("\nPuis : la traduction, depuis l'onglet PDFs ou : bash traduire.sh")
        return 0


if __name__ == "__main__":
    sys.exit(main())
