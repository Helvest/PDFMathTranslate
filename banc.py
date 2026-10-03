"""Le Banc : mesurer un modele sur nos scenarios, pas en general.

    .venv/Scripts/python.exe -u test_banc.py

Trois parties :

1. catalogue()   — tout ce que le proxy dit d'un modele. C'est la source de
                    verite : on ne devine jamais ce qu'un modele supporte, on
                    demande. /v1/models donne supported_parameters,
                    reasoning.supported_efforts, context_length, pricing...

2. un appel      — mesurer : latence, tokens, finish_reason, et surtout ce qui
                    casse en prod (JSON tronque, balises {v*} perdues, reflexion
                    qui fuit, sortie identique a l'entree).

3. les scenarios  — traduction, glossaire, contexte, polices. Les prompts
                    viennent de analyser.py, jamais recopiés : si la prod change,
                    le banc mesure la prod.

Le detail qui a coute une journee : `reasoning: {"enabled": true}` sans effort
donne effort=max sur space-bunny. Le banc lit reasoning.supported_efforts et
par defaut choisit le plus bas qui marche — sinon on mesure le modele le plus
lent du monde et on conclude qu'il est lent.
"""
from __future__ import annotations

import json
import re
import statistics
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import agents as _agents
import analyser as _an
import pymupdf

REPO = Path(__file__).resolve().parent
GLOBAL = REPO.parent / "Global"
BENCH = GLOBAL / "banc"
RESULTATS = BENCH / "resultats.json"

PROXY = "http://127.0.0.1:8645/v1"
CLE = "hermes"
SOURCE = REPO.parent / "Projets" / "Cloud-Empress" / "source"

RE_FORMULE = re.compile(r"\{[^{}]*\}")

# Les levers qu'on regle. Les autres sont affiches en lecture seule : on veut
# les voir pour savoir qu'ils existent, pas les modifier.
LEVIERS_REGLABLES = ("reasoning", "effort", "max_tokens", "temperature")


# ---------------------------------------------------------------- catalogue

def catalogue() -> list[dict]:
    """Tout ce que le proxy dit des modeles, tel quel.

    On ne filtre pas sur ":free" : le choix du modele se fait dans l'interface,
    avec les metadonnees sous les yeux.
    """
    req = urllib.request.Request(f"{PROXY}/models",
                                 headers={"Authorization": f"Bearer {CLE}"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            data = json.load(r)
    except Exception as e:  # noqa: BLE001
        return [{"id": f"proxy injoignable : {e}", "erreur": True}]

    out = []
    for m in data.get("data", []):
        r = m.get("reasoning") or {}
        out.append({
            "id": m.get("id", ""),
            "nom": m.get("name", ""),
            "description": (m.get("description") or "").strip(),
            "contexte": m.get("context_length"),
            "max_completion": (m.get("top_provider") or {}).get("max_completion_tokens"),
            "parametres": m.get("supported_parameters") or [],
            "modalites": (m.get("architecture") or {}).get("input_modalities") or [],
            "raisonnement_obligatoire": bool(r.get("mandatory")),
            "efforts": r.get("supported_efforts") or [],
            "effort_defaut": r.get("default_effort"),
            "gratuit": (m.get("pricing") or {}).get("prompt") == "0.0000000000",
            "expire": m.get("expiration_date"),
            "modere": bool((m.get("architecture") or {}).get("is_moderated")),
            "per_request": m.get("per_request_limits"),
        })
    return out


def effort_serieux(modele: dict) -> str | None:
    """L'effort a utiliser pour ce modele, ou None s'il n'y a rien a choisir.

    space-bunny a mandatory=True et default_effort=max : sans choix explicite
    on declenche 271s par appel et un JSON tronque. On prend donc le plus bas
    effort disponible, que la production utilise deja avec succes.

    Beaucoup de modeles imposent le raisonnement sans lister leurs niveaux
    (minimax, deepseek-r1, les qwen-thinking...). Pour eux on ne peut pas
    choisir : on renvoie None et l'appel part sans effort, ce que le modele
    accepte. Envoyer "aucun" comme valeur ferait refuser la requete entiere —
    c'est exactement l'erreur "reasoning.effort: Invalid option".
    """
    if not modele.get("efforts"):
        return None
    return _an.EFFORT if _an.EFFORT in modele["efforts"] else modele["efforts"][-1]


# ---------------------------------------------------------------- appel

def _construire(modele: dict, leviers: dict, prompt: str) -> dict:
    """Le corps de la requete. Un seul endroit pour tous les leviers."""
    corps = {
        "model": modele["id"],
        "messages": [{"role": "user", "content": prompt}],
    }
    if leviers.get("max_tokens"):
        corps["max_tokens"] = int(leviers["max_tokens"])
    if leviers.get("temperature") is not None:
        corps["temperature"] = leviers["temperature"]
    if leviers.get("effort") and modele.get("efforts"):
        corps["reasoning"] = {"enabled": True, "effort": leviers["effort"]}
    elif leviers.get("effort") and modele.get("raisonnement_obligatoire"):
        # le modele impose le raisonnement mais ne liste pas ses niveaux :
        # on demande le raisonnement sans effort
        corps["reasoning"] = {"enabled": True}
    elif modele.get("raisonnement_obligatoire") and leviers.get("sans_effort", True):
        corps["reasoning"] = {"enabled": True}
    return corps


def appeler(modele: dict, prompt: str, leviers: dict, timeout: int = 180) -> dict:
    """Un appel, avec toutes les mesures."""
    req = urllib.request.Request(
        f"{PROXY}/chat/completions",
        data=json.dumps(_construire(modele, leviers, prompt)).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {CLE}"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode()).get("message", "")[:120]
        except Exception:  # noqa: BLE001
            detail = f"HTTP {e.code}"
        return {"ok": False, "erreur": detail, "latence": round(time.time() - t0, 1)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erreur": f"{type(e).__name__} apres {time.time()-t0:.0f}s",
                "latence": round(time.time() - t0, 1)}

    msg = d["choices"][0]["message"]
    u = d.get("usage", {}) or {}
    det = u.get("completion_tokens_details", {}) or {}
    return {
        "ok": True,
        "sortie": (msg.get("content") or "").strip(),
        "reflexion_fuite": bool((msg.get("content") or "").lstrip().startswith("<")),
        "a_raisonne": bool(msg.get("reasoning")),
        "latence": round(time.time() - t0, 1),
        "tok_entree": u.get("prompt_tokens"),
        "tok_sortie": u.get("completion_tokens"),
        "tok_raisonnement": det.get("reasoning_tokens"),
        "finish": d["choices"][0].get("finish_reason"),
        "tronce": d["choices"][0].get("finish_reason") == "length",
    }


# ---------------------------------------------------------------- qualite

MARQUEURS_REFLEXION = ("<think>", "<reasoning>", "<thinking>",
                        "<|thinking|>", "**Reflection", "\\u00e9\\u0301tude")


def _fuite_reflexion(sortie: str) -> bool:
    """Le modele a-t-il laisse sa reflexion dans la reponse ?

    On detecte sur la sortie elle-meme, pas sur un drapeau fourni par
    l'appelant : c'est la seule facon d'attraper une fuite quand l'appel vient
    d'ailleurs (interface, test, autre outil).
    """
    t = sortie.lstrip()
    if any(t.startswith(m) for m in MARQUEURS_REFLEXION):
        return True
    # certains modeles ouvrent par "Voici ma reflexion :" en clair
    return t.lower().startswith(("voici ma reflexion", "my reasoning",
                                 "let me think", "reasoning:"))


def _json_valide(texte: str) -> bool:
    """Du JSON exploitable : objet ou tableau, pas un fragment coupe."""
    i, j = texte.find("{"), texte.rfind("}")
    i2, j2 = texte.find("["), texte.rfind("]")
    if i < 0 or j <= i:
        if i2 < 0 or j2 <= i2:
            return False
        i, j = i2, j2
    try:
        json.loads(texte[i:j + 1])
        return True
    except json.JSONDecodeError:
        return False


def _compter_polices(texte: str) -> int:
    """Combien de polices le modele a vraiment traitees.

    Chercher un [] vide n'est pas une erreur : sans consigne, le modele a
    legitimately rien a chercher. On compte donc les entrees traitees.
    """
    i, j = texte.find("{"), texte.rfind("}")
    if i < 0 or j <= i:
        return 0
    try:
        d = json.loads(texte[i:j + 1])
    except json.JSONDecodeError:
        return 0
    par = d.get("par_police") or {}
    return len(par) if isinstance(par, dict) else 0


def noter(scenario: str, source: str, r: dict) -> dict:
    """Ce qui casse reellement une traduction ou un glossaire.

    - balises    : {v*} de babeldoc doivent survivre
    - ratio      : une traduction FR fait ~1,15x la longueur EN
    - identique  : sortie == entree, donc rien n'a ete fait
    - json       : la reponse est-elle exploitable (glossaire, polices)
    - tronce     : finish_reason == length, le JSON est coupe en plein
    """
    if not r.get("ok"):
        return {"ok": False, "erreur": r.get("erreur")}

    sortie = r.get("sortie", "")
    src = source.strip()
    bal = RE_FORMULE.findall(src)
    bal_ok = sum(1 for x in bal if x in sortie)
    ratio = round(len(sortie) / max(len(src), 1), 2) if src else None

    n = {
        "ok": True,
        "latence": r["latence"],
        "tok_entree": r.get("tok_entree"),
        "tok_sortie": r.get("tok_sortie"),
        "tok_raisonnement": r.get("tok_raisonnement"),
        "finish": r.get("finish"),
        "tronce": r.get("tronce", False),
        "reflexion_fuite": _fuite_reflexion(sortie),
        "balises": f"{bal_ok}/{len(bal)}" if bal else "n/a",
        "ratio": ratio,
        "identique": sortie.strip().lower() == src.lower(),
        "longueur": len(sortie),
    }

    if scenario == "glossaire":
        # les sous-agents rendent un tableau [{src, tgt, definition}]
        termes = _agents.lire_reponse(sortie)
        n["termes"] = len(termes)
        # un glossaire sans terme n'est pas un glossaire, meme si la reponse
        # est du JSON valide
        n["exploitable"] = bool(termes) and not n["tronce"]

    elif scenario == "polices":
        # les polices rendent un objet {sites, par_police, interdits, notes},
        # pas un tableau : lire_reponse() ne s'y applique pas
        n["termes"] = _compter_polices(sortie)
        n["exploitable"] = _json_valide(sortie) and not n["tronce"]

    else:
        n["exploitable"] = bool(sortie) and not n["tronce"] and not n["identique"]

    return n


# ---------------------------------------------------------------- scenarios

def _page_de_test() -> tuple[str, str, str]:
    """Une vraie page d'un vrai PDF, et son nom."""
    if not SOURCE.is_dir():
        return ("Texte de test.\n\nThe Lowland Wastes hold many secrets.", "test.pdf", "1")
    pdf = sorted(SOURCE.glob("*.pdf"))[0]
    doc = pymupdf.open(str(pdf))
    txt = doc[0].get_text()
    doc.close()
    return txt, pdf.name, "1"


def prompt_scenario(scenario: str, texte: str, doc: str, page: str) -> str:
    """Le prompt d'un scenario, pris tel quel dans la production.

    Ce n'est pas une copie : c'est la constante de analyser.py, donc si la
    production change, le banc mesure la production.
    """
    consignes = "(l'utilisateur n'en a pas ecrit)"

    if scenario == "traduction":
        # le vrai prompt de traduction est construit par babeldoc ; on prend
        # le meme principe que bench.py
        return (
            "You are a professional, authentic machine translation engine. "
            "Only output the translated text, do not include any other text.\n\n"
            "Translate the following markdown source text to French. "
            "Keep the formula notation {v*} unchanged. "
            "Output translation directly without any additional text.\n\n"
            f"Source Text: {texte}"
        )

    if scenario == "glossaire":
        return _agents.prompt_extraction(texte)

    if scenario == "contexte":
        return _an.CTX_PAGE_PROMPT.format(
            consignes=consignes, lot_ctx="(lot de 5 documents)",
            doc_ctx="(resume du document)", doc=doc, page=page,
            total="8", text=texte)

    if scenario == "polices":
        return _an.CONSIGNES_PROMPT.format(
            consignes=consignes,
            polices="FuturaPT-Book, Optima, Caslon",
            contexte="Un jeu de role dans un monde post-apocalyptique.")

    raise ValueError(f"scenario inconnu : {scenario}")


SCENARIOS = ("traduction", "glossaire", "contexte", "polices")


# ---------------------------------------------------------------- execution

def mesurer(modele: dict, scenario: str, leviers: dict, texte: str,
            doc: str, page: str, timeout: int = 180) -> dict:
    """Un modele, un scenario, une mesure."""
    prompt = prompt_scenario(scenario, texte, doc, page)
    r = appeler(modele, prompt, leviers, timeout)
    n = noter(scenario, texte, r)
    n["scenario"] = scenario
    n["modele"] = modele["id"]
    n["prompt_car"] = len(prompt)
    return n


def _resume(reponses: list[dict]) -> dict:
    """Un modele passe sur tous les scenarios."""
    ok = [r for r in reponses if r.get("ok")]
    lats = [r["latence"] for r in ok]
    sorties = [r for r in ok if r.get("tok_sortie")]
    return {
        "reussis": f"{len(ok)}/{len(reponses)}",
        "lat_moy": round(statistics.mean(lats), 1) if lats else None,
        "lat_max": round(max(lats), 1) if lats else None,
        "tok_sortie_moy": round(statistics.mean(
            r["tok_sortie"] for r in sorties)) if sorties else None,
        "tronces": sum(1 for r in ok if r.get("tronce")),
        "exploitables": f"{sum(1 for r in ok if r.get('exploitable'))}/{len(ok)}",
    }


def lancer(modeles: list[dict], leviers: dict, scenarios=SCENARIOS,
           timeout: int = 180) -> dict:
    """Tous les modeles, tous les scenarios, en parallele."""
    texte, doc, page = _page_de_test()
    tasks = [(m, s) for m in modeles for s in scenarios]
    brut: dict[tuple[str, str], dict] = {}

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {
            ex.submit(mesurer, m, s, leviers, texte, doc, page, timeout): (m["id"], s)
            for m, s in tasks
        }
        for f in as_completed(futs):
            m, s = futs[f]
            brut[(m, s)] = f.result()

    par_modele = []
    for m in modeles:
        reps = [brut.get((m["id"], s), {}) for s in scenarios]
        ligne = {"modele": m["id"], "nom": m.get("nom", ""),
                 "effort": leviers.get("effort", "aucun"),
                 **_resume(reps)}
        for s, r in zip(scenarios, reps):
            ligne[s] = r
        par_modele.append(ligne)

    return {
        "maj": datetime.now().isoformat(timespec="seconds"),
        "leviers": leviers,
        "texte_car": len(texte),
        "pdf": doc,
        "modeles": par_modele,
    }


def charger() -> dict:
    if RESULTATS.is_file():
        try:
            return json.loads(RESULTATS.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"modeles": [], "maj": None}


def _auto_test() -> int:
    cat = catalogue()
    print(f"  {len(cat)} modeles\n")
    for m in cat[:6]:
        if m.get("erreur"):
            print("  ", m["id"])
            continue
        print(f"  {m['id']:42} ctx={m['contexte']} "
              f"efforts={','.join(m['efforts']) or '-'} "
              f"defaut={m['effort_defaut'] or '-'} "
              f"reglables={'OUI' if m['parametres'] else 'non'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_auto_test())