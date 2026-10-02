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


def _chercher_manquantes(found: dict[str, dict], font_dir: Path) -> None:
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
        # "FuturaPT-Book" -> on cherche la famille, plus efficace
        famille = found[nom]["famille"] or nom
        res = dl.telecharger(famille)
        if res.ok:
            trouvees += 1
            found[nom]["piste"] = res.archive.name if res.archive else ""
            noms = ", ".join(f.name for f in res.fichiers[:3])
            archive = res.archive.name if res.archive else "(archive inconnue)"
            print(f"    {nom:24} -> {archive} ({len(res.fichiers)} fichiers)")
            print(f"      {'':24}    {noms}")
        else:
            print(f"    {nom:24} -> rien trouve ({res.erreur})")

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
) -> None:
    """Polices : aucun LLM, instantane.

    Si auto_download, les polices sans remplacement sont cherchees sur
    dafontfree.io. Les archives vont dans Work/downloads/ ; rien n'est
    installe automatiquement dans polices/ (c'est un choix manuel).
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
        if name in existing and existing[name].get("fichier_remplacement"):
            e["fichier"] = existing[name]["fichier_remplacement"]
        elif (font_dir / f"{name}.ttf").exists():
            e["fichier"] = f"{name}.ttf"
            prefilled += 1
        elif (font_dir / f"{name}.otf").exists():
            e["fichier"] = f"{name}.otf"
            prefilled += 1
        else:
            e["fichier"] = existing.get(name, {}).get("fichier_remplacement", "")

        # Un fichier_remplacement qui ne pointe plus sur rien est efface :
        # sinon le CSV annonce une police absente et polices.py la signale
        # comme introuvable a chaque run.
        if e["fichier"] and not _fichier_present(e["fichier"], font_dir):
            e["fichier"] = ""
    if prefilled:
        print(f"  {prefilled} pre-remplies depuis polices/")

    # recherche automatique des polices non remplacees
    if auto_download:
        _chercher_manquantes(found, font_dir)

    # fusion : existant d'abord, puis les nouvelles polices
    rows, seen = [], set()
    for name, r in existing.items():
        if name in found:
            rows.append(
                {
                    "police_origine": name,
                    "famille": r.get("famille") or found[name]["famille"],
                    "fichier_remplacement": found[name]["fichier"],
                    "source_pdf": "|".join(sorted(found[name]["pdfs"])),
                }
            )
            seen.add(name)
    added = 0
    for name in sorted(found):
        if name in seen:
            continue
        added += 1
        rows.append(
            {
                "police_origine": name,
                "famille": found[name]["famille"],
                "fichier_remplacement": found[name]["fichier"],
                "source_pdf": "|".join(sorted(found[name]["pdfs"])),
            }
        )
    write_csv(csv_path, ["police_origine", "famille", "fichier_remplacement", "source_pdf"], rows)
    print(f"  +{added} nouvelles -> {csv_path.name} ({len(rows)} lignes)")


CTX_PAGE_PROMPT = """Tu analyses un document pour preparer sa traduction.

Contexte global connu jusqu'ici :
{global_ctx}

Voici le texte de la page {page}/{total} de "{doc}" :

---
{text}
---

Reponds en 3 lignes maximum, format exact :
GLOBAL: <la ligne a AJOUTER au contexte global si cette page apporte une information
nouvelle et durable sur le document (univers, ton, personnages, terminologie).
Si rien de nouveau, ecris exactement RIEN>
PAGE: <resume en une phrase de ce que contient cette page>
"""

CTX_EMPTY = "GLOBAL: RIEN\nPAGE: (page sans contenu textuel exploitable)"


def step_context(pdfs: list[Path], work: Path, ctx_path: Path) -> str:
    """Passe 1 : contexte global, mis a jour page apres page."""
    print("\n[2/3] Contexte (passe 1, LLM)")
    if ctx_path.exists():
        print("  contexte.md deja present -> reutilise tel quel (supprimer pour refaire)")
        return ctx_path.read_text(encoding="utf-8")

    global_lines: list[str] = []
    per_page: list[str] = []
    for pdf in pdfs:
        pages = [p for p in pdf_pages(pdf)]
        print(f"  {pdf.name}: {len(pages)} pages")
        for n, txt in enumerate(pages, 1):
            if len(txt) < MIN_PAGE_CHARS:
                per_page.append(f"### {pdf.name} p{n}\n{CTX_EMPTY.splitlines()[1][6:]}\n")
                continue
            raw = llm(
                CTX_PAGE_PROMPT.format(
                    global_ctx="\n".join(global_lines) or "(aucun pour l'instant)",
                    page=n,
                    total=len(pages),
                    doc=pdf.name,
                    text=txt[:8000],
                )
            )
            g, p = "", ""
            for line in raw.splitlines():
                if line.startswith("GLOBAL:"):
                    g = line[7:].strip()
                elif line.startswith("PAGE:"):
                    p = line[5:].strip()
            if g and g.upper() not in ("RIEN", "NONE", "-"):
                global_lines.append(f"- {g}")
                print(f"    p{n}: +contexte global")
            per_page.append(f"### {pdf.name} p{n}\n{p or '(vide)'}\n")

    out = ["# Contexte du lot\n", "## Description globale\n"]
    out += [g if g.startswith("-") else f"- {g}" for g in global_lines] or ["(vide)"]
    out += ["\n## Description par page\n"] + per_page
    ctx_path.write_text("\n".join(out), encoding="utf-8")
    print(f"  -> {ctx_path.name} ({len(global_lines)} lignes globales)")
    return "\n".join(global_lines)


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
    ap = argparse.ArgumentParser(description="Passe d'analyse d'un lot de PDFs")
    ap.add_argument("source", nargs="?", default=None, help="dossier des PDFs (defaut: ../Work/source)")
    ap.add_argument("-o", "--output", default=None, help="dossier de travail (defaut: ../Work/analyse)")
    ap.add_argument("-lo", "--lang-out", default="fr")
    ap.add_argument("--skip-context", action="store_true")
    ap.add_argument("--skip-glossary", action="store_true")
    ap.add_argument("--skip-fonts", action="store_true")
    ap.add_argument(
        "--no-download",
        action="store_true",
        help="ne pas chercher les polices manquantes sur dafontfree.io",
    )
    a = ap.parse_args()

    # defauts relatifs au depot : le dossier de travail est ../Work (hors depot)
    repo = Path(__file__).resolve().parent
    src = Path(a.source) if a.source else repo.parent / "Work" / "source"
    work = Path(a.output) if a.output else repo.parent / "Work" / "analyse"
    pdfs = sorted(src.glob("*.pdf"))
    if not pdfs:
        print(f"aucun PDF dans {src}", file=sys.stderr)
        return 1

    # proxy obligatoire
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

    if not a.skip_fonts:
        step_fonts(
            pdfs, work, work / "polices.csv", font_dir, auto_download=not a.no_download
        )
    if not a.skip_context:
        ctx = step_context(pdfs, work, work / "contexte.md")
    else:
        ctx = ""
    if not a.skip_glossary:
        step_glossary(pdfs, work, work / "glossaire.csv", ctx, a.lang_out)

    print("\nTermine. A verifier/corriger :")
    print(f"  {work/'glossaire.csv'}")
    print(f"  {work/'polices.csv'}")
    print(f"  {work/'polices'}  (depose tes .ttf ici)")
    print(f"\nPuis : bash traduire.sh   (apres branchement de ces fichiers)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
