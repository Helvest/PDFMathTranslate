#!/usr/bin/env python3
"""Passe d'analyse d'un lot de PDFs : contexte, glossaire, polices.

Produit dans <projet>/analyse/ :
  contexte/      un .json par PDF (resume, points, termes, pages) + _lot.json
  glossaire.csv  source,target,tgt_lng,source_pdf   (format babeldoc + origine)
  polices.csv    police_origine,famille,fichier_remplacement,source_pdf
  polices/       dossier ou deposer les TTF de remplacement

Relancer le script ne detruit RIEN : les entrees deja presentes (donc deja
corrigees a la main) sont conservees, seules les nouveautes sont ajoutees.

Usage:
  .venv/Scripts/python.exe analyser.py                     # <projet>/source/
  .venv/Scripts/python.exe analyser.py "chemin/source" -o "chemin/travail"
"""
from __future__ import annotations

import argparse
import csv
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
MODEL = os.environ.get("PDF2ZH_MODEL", "inclusionai/ling-3.0-flash-sante:free")

# Au dela, une page est decoupee en morceaux (le LLM perd le fil sur du long texte).
CHUNK_CHARS = 5000
# Sous ce nombre de caracteres, une page n'a rien a extraire.
MIN_PAGE_CHARS = 40
TIMEOUT = 300

# ---------------------------------------------------------------- LLM


def llm(prompt: str) -> str:
    """Un appel au proxy Hermes. Retourne le texte, chaine vide si echec."""
    body = json.dumps(
        {
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "reasoning_effort": "none",
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
        return (d["choices"][0]["message"].get("content") or "").strip()
    except urllib.error.HTTPError as e:
        print(f"    ! HTTP {e.code}: {e.read().decode()[:120]}", file=sys.stderr)
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
    csv_path: Path,
    font_dir: Path,
    auto_download: bool = True,
    contexte: str = "",
) -> None:
    """Polices : aucun LLM pour la detection, instantane.

    Si auto_download, les polices sans remplacement sont cherchees sur les
    trois sources. Les archives vont dans <projet>/downloads/ ; rien n'est
    installe automatiquement dans polices/ (c'est un choix manuel).

    Colonnes du CSV, dans l'ordre ou on les lit a la main :
      police_origine      la police du PDF (remplie par le script)
      remplacement        le fichier choisi (a remplir, ou pre-rempli)
      origine             auto | manuel | defaut
      famille             regroupement, sert a la recherche
      pdfs                quels PDF l'utilisent (informatif, en dernier)
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
    existing: dict[str, dict] = {}
    if csv_path.exists():
        with csv_path.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("police_origine"):
                    existing[row["police_origine"]] = row
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
    rows, seen = [], set()
    for name, r in existing.items():
        if name not in found:
            continue
        e = found[name]
        ancien_fichier = (r.get("remplacement") or "").strip()
        ancien_propose = (r.get("propose") or "").strip()

        # option A : si l'utilisateur a change la valeur par rapport a ce que
        # le systeme avait propose, la ligne devient "manuel".
        if ancien_fichier and ancien_fichier != ancien_propose and ancien_propose:
            origine = "manuel"
        elif ancien_fichier and _fichier_present(ancien_fichier, font_dir):
            origine = "manuel"
        elif e.get("propose"):
            origine = "auto"
        else:
            origine = "defaut"

        rows.append(
            {
                "police_origine": name,
                "remplacement": e["fichier"],
                "origine": origine,
                "propose": e.get("propose", ""),
                "raison": e.get("raison", ""),
                "famille": r.get("famille") or e["famille"],
                "spans": e.get("spans", 0),
                "pages": len(e.get("pages", set())),
                "nb_pdfs": len(e["pdfs"]),
                "pdfs": _resumer_pdfs(e["pdfs"]),
            }
        )
        seen.add(name)

    added = 0
    for name in sorted(found):
        if name in seen:
            continue
        added += 1
        e = found[name]
        rows.append(
            {
                "police_origine": name,
                "remplacement": e["fichier"],
                "origine": e["origine"],
                "propose": e.get("propose", ""),
                "raison": e.get("raison", ""),
                "famille": e["famille"],
                "spans": e.get("spans", 0),
                "pages": len(e.get("pages", set())),
                "nb_pdfs": len(e["pdfs"]),
                "pdfs": _resumer_pdfs(e["pdfs"]),
            }
        )

    write_csv(
        csv_path,
        [
            "police_origine",
            "remplacement",
            "origine",
            "propose",
            "raison",
            "famille",
            "spans",
            "pages",
            "nb_pdfs",
            "pdfs",
        ],
        rows,
    )
    n_auto = sum(1 for r in rows if r["origine"] == "auto")
    n_man = sum(1 for r in rows if r["origine"] == "manuel")
    n_def = sum(1 for r in rows if r["origine"] == "defaut")
    print(
        f"  +{added} nouvelles -> {csv_path.name} ({len(rows)} lignes : "
        f"{n_auto} auto, {n_man} manuel, {n_def} defaut)"
    )


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


def _lire_contexte_pdf(chemin: Path) -> dict | None:
    """Lit un contexte JSON. None si absent ou illisible."""
    if not chemin.is_file():
        return None
    try:
        return json.loads(chemin.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"  {chemin.name} illisible ({e}) -> recalcule", file=sys.stderr)
        return None


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


def step_context(pdfs: list[Path], work: Path, ctx_dir: Path) -> list[dict]:
    """Passe 1 : contexte a TROIS niveaux, un fichier JSON par PDF.

    - lot  : commun a tous les documents, enrichi en traversant les PDFs
    - doc  : resume du document
    - page : resume de chaque page

    Chaque PDF a son fichier <nom>.json dans ctx_dir. Un fichier deja present
    est REUTILISE tel quel : supprime-le pour le recalculer.

    Retourne la liste des contextes (pour le glossaire et les polices).
    """
    print("\n[2/3] Contexte (passe 1, LLM) - 3 niveaux : lot / document / page")

    consignes = _lire_consignes(work / "consignes-contexte.md")
    if consignes:
        print("  consignes-contexte.md lu")

    ctx_dir.mkdir(parents=True, exist_ok=True)

    # lot persistant : il s'enrichit d'un run a l'autre
    lot_path = ctx_dir / "_lot.json"
    lot_lines: list[str] = []
    if lot_path.is_file():
        try:
            lot_lines = json.loads(lot_path.read_text(encoding="utf-8")).get("points", [])
        except (json.JSONDecodeError, OSError):
            pass
        if lot_lines:
            print(f"  lot existant : {len(lot_lines)} points (conserves)")

    total_pages = sum(len(pdf_pages(p)) for p in pdfs)
    fait = 0
    ctxs: list[dict] = []

    for pdf in pdfs:
        cible = ctx_dir / f"{pdf.stem}.json"
        deja = _lire_contexte_pdf(cible)
        if deja:
            nb = len(deja.get("pages", {}))
            print(f"  {pdf.name} : contexte deja present ({nb} pages) -> reutilise")
            ctxs.append(deja)
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
        cible.write_text(
            json.dumps(ctx, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"    -> {cible.name} ({len(pages_resume)} pages)")
        ctxs.append(ctx)

    # meme convention que les fichiers de PDF : sans le prefixe "- "
    lot_path.write_text(
        json.dumps(
            {"points": [l[2:] if l.startswith("- ") else l for l in lot_lines]},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"  -> _lot.json : {len(lot_lines)} points")
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
    pdfs: list[Path], work: Path, gloss_path: Path, ctxs: list[dict], lang_out: str
) -> None:
    """Passe 2 : extraction du glossaire, page par page, avec le contexte."""
    print("\n[3/3] Glossaire (passe 2, LLM)")

    # les consignes de l'utilisateur s'appliquent aussi ici : s'il a impose une
    # traduction, le glossaire doit la reprendre a l'identique.
    consignes = _lire_consignes(work / "consignes-contexte.md")
    if consignes:
        print("  consignes-contexte.md lu")

    existing: dict[str, dict] = {}
    if gloss_path.exists():
        with gloss_path.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("source"):
                    existing[row["source"]] = dict(row)
        print(f"  {len(existing)} termes deja presents (conserves tels quels)")

    origin: dict[str, set] = {}
    pages_src: dict[str, set] = {}
    found: dict[str, str] = {}
    total_pages = sum(len(pdf_pages(p)) for p in pdfs)
    fait = 0
    # contexte du lot : la vue condensee de tous les documents
    global_ctx = _resume_lot(ctxs)

    for pdf in pdfs:
        pages = pdf_pages(pdf)
        # le contexte de CE pdf, et le resume de chaque page
        ctx_pdf = next((c for c in ctxs if c.get("fichier") == pdf.name), {})
        res_pages = ctx_pdf.get("pages", {})
        for n, txt in enumerate(pages, 1):
            fait += 1
            prog(fait, total_pages, f"{pdf.name} p{n}")
            if len(txt) < MIN_PAGE_CHARS:
                continue
            page_ctx = (
                f"{pdf.name} page {n} : {res_pages.get(str(n), '')}"
                if res_pages.get(str(n))
                else f"{pdf.name} page {n}"
            )
            for part, ch in enumerate(chunks(txt), 1):
                known = ", ".join(sorted(found)[:60]) or "(aucun)"
                raw = llm(
                    GLOSS_PROMPT.format(
                        consignes=consignes[:6000] or "(aucune consigne fournie)",
                        lang_out=lang_out,
                        global_ctx=global_ctx or "(aucun)",
                        page_ctx=page_ctx + (f" (partie {part})" if part > 1 else ""),
                        known=known,
                        text=ch,
                    )
                )
                added = 0
                for item in parse_json_array(raw):
                    if not isinstance(item, dict):
                        continue
                    src = str(item.get("src", "")).strip()
                    tgt = str(item.get("tgt", "")).strip()
                    if not src or not tgt or len(src) > 80:
                        continue
                    origin.setdefault(src, set()).add(pdf.name)
                    pages_src.setdefault(src, set()).add(f"{pdf.name}:{n}")
                    if src in existing:
                        continue  # deja valide a la main -> jamais ecrase
                    if src in found:
                        continue
                    found[src] = tgt
                    added += 1
                if added:
                    print(f"    {pdf.name} p{n}: +{added}")

    # --- comptage : combien de fois chaque terme apparait, sur combien de pages
    # On compte dans TOUS les PDF du projet, pas seulement ceux de ce run :
    # sinon un terme d'un autre PDF afficherait 0 et le chiffre serait faux.
    print("  comptage des occurrences...")
    dossier_source = work.parent / "source"
    tous = sorted(dossier_source.glob("*.pdf")) if dossier_source.is_dir() else pdfs
    textes: dict[str, list[str]] = {p.name: pdf_pages(p) for p in tous}

    def _compter(terme: str, _pdfs_du_terme: set) -> tuple[int, int]:
        """(occurrences, pages) sur l'ensemble des PDF du projet."""
        total = pages_vues = 0
        for pages in textes.values():
            n, np_ = compter_terme(terme, pages)
            total += n
            pages_vues += np_
        return total, pages_vues

    # un terme deja present garde son pdf d'origine, meme s'il reapparait ailleurs
    def _fusion(src: str, ancien: str) -> str:
        vus = set(filter(None, ancien.split("|")))
        vus |= origin.get(src, set())
        return "|".join(sorted(vus))

    rows, seen = [], set()
    for src, ligne in existing.items():
        rows.append(
            {
                "source": src,
                "target": ligne.get("target", ""),
                "tgt_lng": ligne.get("tgt_lng", lang_out) or lang_out,
                "source_pdf": _fusion(src, ligne.get("source_pdf", "")),
                "pages": "|".join(sorted(pages_src.get(src, set()))),
                "origine": ligne.get("origine") or "auto",
                "occurrences": _compter(src, origin.get(src, set()))[0],
                "nb_pages": _compter(src, origin.get(src, set()))[1],
            }
        )
        seen.add(src)
    for src in sorted(found, key=str.lower):
        if src in seen:
            continue
        rows.append(
            {
                "source": src,
                "target": found[src],
                "tgt_lng": lang_out,
                "source_pdf": _fusion(src, ""),
                "pages": "|".join(sorted(pages_src.get(src, set()))),
                "origine": "auto",
                "occurrences": _compter(src, origin.get(src, set()))[0],
                "nb_pages": _compter(src, origin.get(src, set()))[1],
            }
        )
    write_csv(
        gloss_path,
        [
            "source",
            "target",
            "tgt_lng",
            "source_pdf",
            "pages",
            "origine",
            "occurrences",
            "nb_pages",
        ],
        rows,
    )
    print(f"  +{len(found)} nouveaux -> {gloss_path.name} ({len(rows)} lignes)")


# ---------------------------------------------------------------- io


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" + utf-8-sig : ouvrable directement dans Excel
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


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
    ctx_dir = work / "contexte"
    if a.ignorer_contexte:
        for f in ctx_dir.glob("*.json"):
            f.unlink()
        print("  contextes existants effaces (--ignorer-contexte)")

    if "contexte" in etapes:
        ctxs = step_context(pdfs, work, ctx_dir)
    else:
        ctxs = [
            c
            for f in sorted(ctx_dir.glob("*.json"))
            if not f.name.startswith("_")
            and (c := _lire_contexte_pdf(f)) is not None
        ]
        if ctxs:
            print(f"  {len(ctxs)} contexte(s) PDF relu(s)")

    if "polices" in etapes:
        step_fonts(
            pdfs,
            work,
            work / "polices.csv",
            font_dir,
            auto_download=not a.no_download,
            contexte=_resume_lot(ctxs),
        )
    if "glossaire" in etapes:
        step_glossary(pdfs, work, work / "glossaire.csv", ctxs, a.lang_out)

    print("\nTermine. A verifier/corriger :")
    print(f"  {ctx_dir}  (un .json par PDF + _lot.json)")
    print(f"  {work/'glossaire.csv'}")
    print(f"  {work/'polices.csv'}")
    print(f"  {work/'polices'}  (depose tes .ttf ici)")
    print(f"  {work.parent/'downloads'}  (archives telechargees)")
    print(f"\nPuis : bash traduire.sh   (apres branchement de ces fichiers)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
