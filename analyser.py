#!/usr/bin/env python3
"""Passe d'analyse d'un lot de PDFs : contexte, glossaire, polices.

Produit dans ../Work/analyse/ :
  contexte.md    description globale du lot + une section par page
  glossaire.csv  source,target,tgt_lng,source_pdf   (format babeldoc + origine)
  polices.csv    police_origine,famille,fichier_remplacement,source_pdf
  polices/       dossier ou deposer les TTF de remplacement

Relancer le script ne detruit RIEN : les entrees deja presentes (donc deja
corrigees a la main) sont conservees, seules les nouveautes sont ajoutees.

Usage:
  .venv/Scripts/python.exe analyser.py                     # ../Work/source/
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


def _chercher_manquantes(found: dict[str, dict], font_dir: Path, contexte: str = "") -> None:
    """Cherche sur dafontfree.io les polices sans remplacement.

    N'installe RIEN dans font_dir : les archives restent dans Work/downloads/.
    Le resultat est note dans found[name]['piste'] pour que l'utilisateur
    sache quoi aller chercher.
    """
    manquantes = [n for n, e in found.items() if not e.get("fichier")]
    if not manquantes:
        return

    print(f"  {len(manquantes)} police(s) sans remplacement -> recherche dafontfree.io")
    try:
        import telecharger_polices as dl
    except ImportError:
        print("    (module de telechargement indisponible)")
        return

    trouvees = 0
    for nom in manquantes:
        famille = found[nom]["famille"] or nom
        # le LLM choisit les termes de recherche (noms usuels, synonymes,
        # alternatives libres) plutot qu'un decoupage en dur du nom
        termes = dl._mots_cles(nom, famille, contexte)
        print(f"    {nom:24} termes : {', '.join(termes) or '(aucun)'}")

        res = dl.telecharger(nom, termes=termes)
        if res.ok:
            trouvees += 1
            # On propose le CHEMIN du fichier de police utilisable, pas
            # l'archive : c'est ce que l'utilisateur copiera dans polices/.
            # L'archive reste dans Work/downloads/ pour reference.
            fichier = res.fichiers[0] if res.fichiers else None
            found[nom]["piste"] = str(res.archive) if res.archive else ""
            found[nom]["propose"] = fichier.name if fichier else ""
            found[nom]["origine"] = "auto"
            archive = res.archive.name if res.archive else "(archive inconnue)"
            print(f"      {'':24} -> {archive} [{res.type_trouve}] ({len(res.fichiers)} fichiers)")
            if fichier:
                print(f"      {'':24}    proposerait : {fichier.name}")
        else:
            print(f"      {'':24} -> rien trouve ({res.erreur})")

    if trouvees:
        print(f"  {trouvees} archive(s) dans Work/downloads/ - a installer a la main")
        print("    pour installer : copier le .ttf voulu dans Work/analyse/polices/")
        print("    puis renseigner fichier_remplacement dans polices.csv")


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
    trois sources. Les archives vont dans Work/downloads/ ; rien n'est
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
            e = found.setdefault(name, {"famille": fam, "pdfs": set()})
            e["pdfs"].add(pdf.name)
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
        _chercher_manquantes(found, font_dir, contexte)

    # --- fusion : existant d'abord, puis les nouvelles polices -------------
    # `origine` : auto = propose par le systeme, manuel = choisi par
    # l'utilisateur, defaut = rien trouve (BabelDOC prend sa police).
    #
    # `remplacement` = ce qui est REELLEMENT installe (fichier present dans
    # polices/). `propose` = ce que la recherche a trouve, a copier depuis
    # Work/downloads/ si on le veut.
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
                "famille": r.get("famille") or e["famille"],
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
                "famille": e["famille"],
                "pdfs": _resumer_pdfs(e["pdfs"]),
            }
        )

    write_csv(
        csv_path,
        ["police_origine", "remplacement", "origine", "propose", "famille", "pdfs"],
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
    Le detail complet reste dans contexte.md.
    """
    noms = sorted(noms)
    if len(noms) <= 2:
        return " | ".join(noms)
    return f"{len(noms)} pdfs"


CTX_PAGE_PROMPT = """Tu analyses un document pour preparer sa traduction.

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
"""

CTX_EMPTY = "(page sans contenu textuel exploitable)"


def _lire_ligne(raw: str, prefixe: str) -> str:
    """Extrait 'PREFIXE: valeur' d'une reponse LLM. '' si absent ou RIEN."""
    for line in raw.splitlines():
        if line.startswith(prefixe):
            v = line[len(prefixe) :].strip()
            if v and v.upper() not in ("RIEN", "NONE", "-", "(rien)"):
                return v
    return ""


def step_context(pdfs: list[Path], work: Path, ctx_path: Path) -> str:
    """Passe 1 : contexte a TROIS niveaux.

    - lot  : commun a tous les documents (univers, campagne, serie)
    - doc  : propre a chaque document
    - page : resume de chaque page

    Le contexte du lot s'enrichit en traversant TOUS les documents ; celui du
    document s'enrichit page apres page. Chacun est re-injecte a l'appel
    suivant, donc les trois niveaux se construisent progressivement.

    Retourne le contexte du lot, utilise ensuite par le glossaire.
    """
    print("\n[2/3] Contexte (passe 1, LLM) - 3 niveaux : lot / document / page")
    if ctx_path.exists():
        print("  contexte.md deja present -> reutilise tel quel (supprimer pour refaire)")
        return ctx_path.read_text(encoding="utf-8")

    lot_lines: list[str] = []
    docs: list[dict] = []  # [{"nom":..., "doc_lines":[...], "pages":[(n, resume)]}]

    for pdf in pdfs:
        pages = pdf_pages(pdf)
        doc_lines: list[str] = []
        pages_resume: list[tuple[int, str]] = []
        print(f"  {pdf.name}: {len(pages)} pages")

        for n, txt in enumerate(pages, 1):
            if len(txt) < MIN_PAGE_CHARS:
                pages_resume.append((n, CTX_EMPTY))
                continue

            raw = llm(
                CTX_PAGE_PROMPT.format(
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

            pages_resume.append((n, page or "(vide)"))

        docs.append({"nom": pdf.name, "doc_lines": doc_lines, "pages": pages_resume})

    # --- ecriture du fichier ------------------------------------------------
    out = ["# Contexte du travail\n"]

    out.append("## 1. Contexte du lot (tous les documents)\n")
    out += lot_lines or ["(vide)"]

    for d in docs:
        out.append(f"\n## 2. Document : {d['nom']}\n")
        out += d["doc_lines"] or ["(vide)"]

    out.append("\n## 3. Description par page\n")
    for d in docs:
        out.append(f"\n### {d['nom']}\n")
        for n, resume in d["pages"]:
            out.append(f"- **p{n}** : {resume}")

    ctx_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(
        f"  -> {ctx_path.name} : {len(lot_lines)} lignes lot, "
        f"{sum(len(d['doc_lines']) for d in docs)} lignes document, "
        f"{sum(len(d['pages']) for d in docs)} pages"
    )
    return "\n".join(lot_lines)


GLOSS_PROMPT = """Tu extrais les termes a traduire de facon consistante vers le {lang_out}.

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

Reponds UNIQUEMENT par un tableau JSON, sans commentaire :
[{{"src": "terme", "tgt": "traduction"}}]
"""


def step_glossary(
    pdfs: list[Path], work: Path, gloss_path: Path, global_ctx: str, lang_out: str
) -> None:
    """Passe 2 : extraction du glossaire, page par page, avec le contexte."""
    print("\n[3/3] Glossaire (passe 2, LLM)")
    existing: dict[str, str] = {}
    if gloss_path.exists():
        with gloss_path.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("source"):
                    existing[row["source"]] = row.get("target", "")
        print(f"  {len(existing)} termes deja presents (conserves tels quels)")

    origin: dict[str, set] = {}
    found: dict[str, str] = {}
    for pdf in pdfs:
        pages = pdf_pages(pdf)
        # contexte par page : lu depuis contexte.md si dispo
        for n, txt in enumerate(pages, 1):
            if len(txt) < MIN_PAGE_CHARS:
                continue
            page_ctx = f"{pdf.name} page {n}"
            for part, ch in enumerate(chunks(txt), 1):
                known = ", ".join(sorted(found)[:60]) or "(aucun)"
                raw = llm(
                    GLOSS_PROMPT.format(
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
                    if src in existing:
                        continue  # deja valide a la main -> jamais ecrase
                    if src in found:
                        continue
                    found[src] = tgt
                    added += 1
                if added:
                    print(f"    {pdf.name} p{n}: +{added}")

    rows, seen = [], set()
    for src, tgt in existing.items():
        rows.append(
            {
                "source": src,
                "target": tgt,
                "tgt_lng": lang_out,
                "source_pdf": "|".join(sorted(origin.get(src, set()))),
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
                "source_pdf": "|".join(sorted(origin.get(src, set()))),
            }
        )
    write_csv(gloss_path, ["source", "target", "tgt_lng", "source_pdf"], rows)
    print(f"  +{len(found)} nouveaux -> {gloss_path.name} ({len(rows)} lignes)")


# ---------------------------------------------------------------- io


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" + utf-8-sig : ouvrable directement dans Excel
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Passe d'analyse d'un lot de PDFs",
        epilog=(
            "Les etapes sont independantes : on peut lancer le contexte sans les "
            "polices, ou les polices sans le contexte. Par defaut les trois."
        ),
    )
    ap.add_argument("source", nargs="?", default=None, help="dossier des PDFs (defaut: ../Work/source)")
    ap.add_argument("-o", "--output", default=None, help="dossier de travail (defaut: ../Work/analyse)")
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

    # defauts relatifs au depot : le dossier de travail est ../Work (hors depot)
    repo = Path(__file__).resolve().parent
    src = Path(a.source) if a.source else repo.parent / "Work" / "source"
    work = Path(a.output) if a.output else repo.parent / "Work" / "analyse"
    pdfs = sorted(src.glob("*.pdf"))
    if not pdfs:
        print(f"aucun PDF dans {src}", file=sys.stderr)
        return 1

    # le proxy n'est requis que pour les etapes qui appellent le LLM
    besoin_llm = bool(etapes & {"contexte", "glossaire"}) or (
        "polices" in etapes and not a.no_download
    )
    if besoin_llm:
        try:
            urllib.request.urlopen(PROXY + "/models", timeout=10)
        except Exception:
            print(f"proxy Hermes injoignable sur {PROXY}", file=sys.stderr)
            print("lance: hermes proxy start --provider nous --port 8645", file=sys.stderr)
            return 1

    work.mkdir(parents=True, exist_ok=True)
    font_dir = work / "polices"
    font_dir.mkdir(exist_ok=True)
    print(f"lot: {len(pdfs)} PDF(s) -> {work}")
    print(f"etapes : {', '.join(sorted(etapes))}")

    # Le contexte nourrit la recherche de polices : on le calcule d'abord si
    # les deux etapes sont demandees. Sinon on relit le fichier existant.
    ctx = ""
    if "contexte" in etapes:
        ctx = step_context(pdfs, work, work / "contexte.md")
    elif "polices" in etapes:
        ctx_path = work / "contexte.md"
        if ctx_path.exists():
            ctx = ctx_path.read_text(encoding="utf-8")

    if "polices" in etapes:
        step_fonts(
            pdfs,
            work,
            work / "polices.csv",
            font_dir,
            auto_download=not a.no_download,
            contexte=ctx,
        )
    if "glossaire" in etapes:
        step_glossary(pdfs, work, work / "glossaire.csv", ctx, a.lang_out)

    print("\nTermine. A verifier/corriger :")
    print(f"  {work/'contexte.md'}")
    print(f"  {work/'glossaire.csv'}")
    print(f"  {work/'polices.csv'}")
    print(f"  {work/'polices'}  (depose tes .ttf ici)")
    print(f"  {work.parent/'downloads'}  (archives telechargees)")
    print(f"\nPuis : bash traduire.sh   (apres branchement de ces fichiers)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
