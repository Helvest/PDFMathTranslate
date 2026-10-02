#!/usr/bin/env python3
"""Test de performances des modeles, pour choisir celui a utiliser.

Ce script mesure ce qui compte pour la traduction, pas un benchmark general :
  - le modele repond-il, et en combien de temps
  - combien de tokens il sort (un modele bavard coute plus cher en temps)
  - s'il laisse echapper sa reflexion (reasoning) dans le texte traduit
  - s'il preserve les balises de formule {v*} que babeldoc utilise
  - si le texte traduit a une longueur plausible (ni tronque, ni noye)

Les resultats vont dans Global/bench/ et sont relus par l'interface.

    python bench.py                       # tout, config par defaut
    python bench.py --modeles a,b         # seulement ces modeles
    python bench.py --configs rapide      # 1 seule config (rapide)
    python bench.py --configs complet     # la grille entiere
    python bench.py --liste               # juste les modeles du proxy

Aucune dependance : urllib et la bibliotheque standard suffisent.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent
GLOBAL = REPO.parent / "Global"
BENCH_DIR = GLOBAL / "bench"
RESULTATS = BENCH_DIR / "resultats.json"
TEXTES = BENCH_DIR / "textes.json"

PROXY = os.environ.get("PDF2ZH_PROXY", "http://127.0.0.1:8645/v1")
CLE = "hermes"

# ---------------------------------------------------------------- configs

# La grille : ce qui influe vraiment sur la traduction. `reasoning=false` est
# ce qu'on veut (le modele ne doit pas reflechir avant de traduire, ca coute
# du temps et ca peut fuir dans la sortie).
CONFIGS = {
    "rapide": {"reasoning": {"enabled": False}, "temperature": 0},
    "sans-reflexion": {"reasoning": {"enabled": False}, "temperature": 0.5},
    "reflexion-minimale": {"reasoning_effort": "minimal", "temperature": 0},
    "creatif": {"reasoning": {"enabled": False}, "temperature": 1},
}
# Le complet ajoute les croisements reasoning minimal x temperatures.
CONFIGS_COMPLET = {
    **CONFIGS,
    "reflexion-minimale-chaude": {"reasoning_effort": "minimal", "temperature": 0.5},
    "reflexion-minimale-creatif": {"reasoning_effort": "minimal", "temperature": 1},
}

# Textes de reference : courts, varies, avec des cas qui piegent les modeles.
TEXTES_DEFAUT = [
    (
        "markdown simple",
        "The Farmerlings tend their fungal gardens beneath the Lowland Wastes.\n\n"
        "**Baktos** watches from the ridge, counting the smoke.",
    ),
    (
        "formules",
        "The damage is {v1} plus {v2} per round.\n"
        "Roll {v3} to resist the spores.",
    ),
    (
        "liste et vocabulaire",
        "1. Holy Jambu - Prickly numbing fruit.\n"
        "2. Immortal Hen - An edible, ever regenerating chicken.\n"
        "3. Crawling Comb - A comb that walks on tiny legs.",
    ),
    (
        "phrase longue",
        "Despite the warnings carved into the bone-white pillars, the expedition "
        "pressed deeper into the containment zone, where the air tasted of copper "
        "and the silence was broken only by the distant hum of something ancient.",
    ),
    (
        "termes a conserver",
        "Cloud Empress rules the Lowland Wastes. The plastisteel tokens of "
        "Farmerlings are traded at the Imago market.",
    ),
    (
        "titres en majuscules",
        "BARRACKS\n\nARMORY\n\nCONTAINMENT\n\nCOFFIN TESTING\n\n"
        "Each room holds its own dangers.",
    ),
]

PROMPT = (
    "You are a professional, authentic machine translation engine. "
    "Only Output the translated text, do not include any other text.\n\n"
    "Translate the following markdown source text to French. "
    # {v*} est double : sinon str.format() le prendrait pour un champ
    "Keep the formula notation {{v*}} unchanged. "
    "Output translation directly without any additional text.\n\n"
    "Source Text: {texte}"
)

RE_FORMULE = re.compile(r"\{[^{}]*\}")


# ---------------------------------------------------------------- proxy

def modeles_disponibles() -> list[str]:
    """Les modeles gratuits proposes par le proxy."""
    try:
        req = urllib.request.Request(
            f"{PROXY}/models", headers={"Authorization": f"Bearer {CLE}"}
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
    except Exception as e:
        print(f"proxy injoignable sur {PROXY} ({e})", file=sys.stderr)
        print("lance: hermes proxy start --provider nous --port 8645", file=sys.stderr)
        return []
    noms = [m.get("id", "") for m in d.get("data", [])]
    return sorted(n for n in noms if n.endswith(":free"))


def appeler(modele: str, texte: str, extra: dict, timeout: int = 300) -> dict:
    """Un appel au proxy, avec mesure du temps et des tokens."""
    payload = {
        "model": modele,
        "messages": [{"role": "user", "content": PROMPT.format(texte=texte)}],
        **extra,
    }
    corps = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{PROXY}/chat/completions",
        data=corps,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {CLE}"},
    )
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
        msg = d["choices"][0]["message"]
        u = d.get("usage", {}) or {}
        return {
            "ok": True,
            "sortie": (msg.get("content") or "").strip(),
            "reflexion": bool(msg.get("reasoning")),
            "latence": round(time.time() - t, 2),
            "tok_entree": u.get("prompt_tokens"),
            "tok_sortie": u.get("completion_tokens"),
        }
    except Exception as e:
        return {
            "ok": False,
            "erreur": f"{type(e).__name__}: {str(e)[:140]}",
            "latence": round(time.time() - t, 2),
        }


# ---------------------------------------------------------------- qualite

def noter(sorties: list[dict], sources: list[str]) -> dict:
    """Notes de qualite, sur ce qui casse une traduction en pratique.

    - balises : les {v*} de babeldoc doivent survivre telles quelles
    - ratio   : une traduction FR fait ~1,15x la longueur EN. Trop court =
                tronque, trop long = le modele a bavarde.
    - fuite   : le modele a-t-il laisse sa reflexion dans le texte
    - anglais: le texte sorti est-il reste en anglais (non-traduction)
    """
    balises_tot = balises_ok = 0
    ratios = []
    fuites = 0
    nontraduits = 0
    n = 0

    for r, src_txt in zip(sorties, sources):
        if not r.get("ok"):
            continue
        n += 1
        s, o = RE_FORMULE.findall(src_txt), RE_FORMULE.findall(r["sortie"])
        balises_tot += len(s)
        balises_ok += sum(1 for x in s if x in o)

        if src_txt.strip():
            ratios.append(len(r["sortie"]) / max(len(src_txt), 1))

        if r.get("reflexion"):
            fuites += 1

        # heuristique : si la sortie est identique a la source, rien n'a ete traduit
        if r["sortie"].strip().lower() == src_txt.strip().lower():
            nontraduits += 1

    return {
        "balises": f"{balises_ok}/{balises_tot}" if balises_tot else "n/a",
        "ratio": round(sum(ratios) / len(ratios), 2) if ratios else None,
        "reflexion_fuite": f"{fuites}/{n}" if n else "n/a",
        "non_traduits": f"{nontraduits}/{n}" if n else "n/a",
    }


# ---------------------------------------------------------------- execution

def tester_config(
    nom: str, extra: dict, modeles: list[str], textes: dict, workers: int = 3
) -> list[dict]:
    """Une config, tous les modeles, tous les textes."""
    print(f"\n### {nom}  ({extra})", flush=True)
    taches = [(m, t) for m in modeles for t in textes]
    brut: dict[str, dict] = {}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futurs = {
            ex.submit(appeler, m, textes[t], extra): (m, t) for m, t in taches
        }
        for f in as_completed(futurs):
            m, t = futurs[f]
            brut.setdefault(m, {})[t] = f.result()

    lignes = []
    for m in modeles:
        rs = brut.get(m, {})
        oks = [r for r in rs.values() if r.get("ok")]
        lat = [r["latence"] for r in oks]
        tok = [r["tok_sortie"] for r in oks if r.get("tok_sortie")]
        note = noter([rs.get(t, {}) for t in textes], list(textes.values()))

        lignes.append({
            "modele": m,
            "ok": f"{len(oks)}/{len(rs)}",
            "lat_moy_s": round(sum(lat) / len(lat), 1) if lat else None,
            "lat_max_s": round(max(lat), 1) if lat else None,
            "lat_totale_s": round(sum(r["latence"] for r in rs.values()), 1),
            "tok_sortie_moy": round(sum(tok) / len(tok)) if tok else None,
            "reflexion_fuite": note["reflexion_fuite"],
            "balises": note["balises"],
            "ratio": note["ratio"],
            "non_traduits": note["non_traduits"],
            "erreur": next((r["erreur"] for r in rs.values() if not r.get("ok")), None),
        })
        l = lignes[-1]
        print(
            f"  {m:44} {l['ok']:6} moy={str(l['lat_moy_s']):6}s "
            f"tok={str(l['tok_sortie_moy']):6} bal={l['balises']:6} "
            f"ratio={l['ratio']}",
            flush=True,
        )
    return lignes


def charger_resultats() -> dict:
    if RESULTATS.is_file():
        try:
            return json.loads(RESULTATS.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"configs": {}, "maj": None}


def main() -> int:
    ap = argparse.ArgumentParser(description="Test de performances des modeles")
    ap.add_argument("--modeles", help="modeles a tester, separes par des virgules")
    ap.add_argument(
        "--configs",
        default="rapide",
        help="rapide (defaut) | complet | noms separes par des virgules",
    )
    ap.add_argument("--liste", action="store_true", help="lister les modeles et sortir")
    ap.add_argument("--workers", type=int, default=3, help="appels en parallele (defaut 3)")
    a = ap.parse_args()

    dispo = modeles_disponibles()
    if not dispo:
        return 1
    if a.liste:
        print(f"{len(dispo)} modeles gratuits :")
        for m in dispo:
            print("  ", m)
        return 0

    # --- modeles a tester
    if a.modeles:
        voulus = [m.strip() for m in a.modeles.split(",") if m.strip()]
        inconnus = [m for m in voulus if m not in dispo]
        if inconnus:
            print(f"modeles inconnus : {', '.join(inconnus)}", file=sys.stderr)
            return 2
        modeles = voulus
    else:
        modeles = dispo

    # --- configs a tester
    if a.configs == "rapide":
        choisies = {"rapide": CONFIGS["rapide"]}
    elif a.configs == "complet":
        choisies = CONFIGS_COMPLET
    else:
        noms = [c.strip() for c in a.configs.split(",") if c.strip()]
        toutes = {**CONFIGS, **CONFIGS_COMPLET}
        inconnues = [c for c in noms if c not in toutes]
        if inconnues:
            print(f"configs inconnues : {', '.join(inconnues)}", file=sys.stderr)
            print(f"disponibles : {', '.join(sorted(toutes))}", file=sys.stderr)
            return 2
        choisies = {c: toutes[c] for c in noms}

    # --- textes de reference (crees au premier lancement)
    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    if TEXTES.is_file():
        try:
            charge = json.loads(TEXTES.read_text(encoding="utf-8"))
            textes = {k: v for k, v in charge.items()}
        except (json.JSONDecodeError, OSError):
            textes = dict(TEXTES_DEFAUT)
    else:
        textes = dict(TEXTES_DEFAUT)
        TEXTES.write_text(
            json.dumps(textes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"textes de reference crees : {TEXTES}")

    print(f"modeles  : {len(modeles)}")
    print(f"configs  : {', '.join(choisies)}")
    print(f"textes   : {len(textes)}")
    print(f"appels   : {len(modeles) * len(choisies) * len(textes)}")

    resultats = charger_resultats()
    debut = time.time()
    for nom, extra in choisies.items():
        resultats["configs"][nom] = tester_config(
            nom, extra, modeles, textes, a.workers
        )
        resultats["maj"] = datetime.now().isoformat(timespec="seconds")
        resultats["modeles_disponibles"] = len(dispo)
        RESULTATS.write_text(
            json.dumps(resultats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    print(f"\nTermine en {round(time.time() - debut)}s -> {RESULTATS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
