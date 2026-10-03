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
CHARGE = BENCH / "charge.json"

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

    Les champs.rename(s) servent a l'affichage ; le modele complet est garde
    dans "brut", intact. La raison : /v1/models evolves, et un jour il y aura un
    champ qu'on n'avait pas prevu. Plutot que de le perdre, on le garde et
    l'interface l'affiche tel quel — on ne decide pas a la place de
    l'utilisateur de ce qui est utile.

    On ne filtre pas sur ":free" : le choix se fait dans l'interface, avec les
    metadonnees sous les yeux.
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
            # ce que l'interface affiche
            "id": m.get("id", ""),
            "nom": m.get("name", ""),
            "description": (m.get("description") or "").strip(),
            "contexte": m.get("context_length"),
            "max_completion": (m.get("top_provider") or {}).get("max_completion_tokens"),
            "parametres": m.get("supported_parameters") or [],
            "modalites": (m.get("architecture") or {}).get("input_modalites") or [],
            "raisonnement_obligatoire": bool(r.get("mandatory")),
            "efforts": r.get("supported_efforts") or [],
            "effort_defaut": r.get("default_effort"),
            "gratuit": (m.get("pricing") or {}).get("prompt") == "0.0000000000",
            "expire": m.get("expiration_date"),
            "modere": bool((m.get("architecture") or {}).get("is_moderated")),
            "per_request": m.get("per_request_limits"),
            # tout le reste, intact : aliases, canonical_slug, knowledge_cutoff,
            # default_parameters, pricing complet, links, supported_voices...
            "brut": m,
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
    """Le corps de la requete. Un seul endroit pour tous les leviers.

    Chaque levier n'est envoye que si l'utilisateur l'a fixe ET que le modele
    le supporte. Envoyer un parametre non supporte, c'est faire refuser la
    requete entiere par le routeur — on ne tente donc rien au hasard.
    """
    supporte = set(modele.get("parametres") or [])
    corps = {
        "model": modele["id"],
        "messages": [{"role": "user", "content": prompt}],
    }

    def _met(nom_api: str, cle: str, conversion=None):
        """N'envoie que si regle ET supporte."""
        v = leviers.get(cle)
        if v is None or v == "":
            return
        # un modele qui ne liste pas supported_parameters ne peut pas etre
        # verifie : on laisse passer, plutot que de tout refuser
        if supporte and nom_api not in supporte:
            return
        corps[nom_api] = conversion(v) if conversion else v

    _met("max_tokens", "max_tokens", int)
    if leviers.get("temperature") is not None:
        corps["temperature"] = leviers["temperature"]
    # ces trois-la changent le fond : la diversite (temperature), la
    # reproductibilite (seed), et l'arret (stop)
    _met("top_p", "top_p")
    _met("seed", "seed", int)
    _met("stop", "stop")
    # la repetition est ce qui fait boucler un modele sur du JSON
    _met("repetition_penalty", "repetition_penalty")

    # include_reasoning est l'inverse de "effort" : il dit si on VOIT le
    # raisonnement dans la reponse. Les deux se combinent.
    inclure = leviers.get("include_reasoning")
    if modele.get("efforts"):
        if leviers.get("effort"):
            corps["reasoning"] = {"enabled": True, "effort": leviers["effort"],
                                  "include_reasoning": bool(inclure)}
    elif modele.get("raisonnement_obligatoire"):
        # le modele impose le raisonnement sans dire ses niveaux : on demande
        # le raisonnement, sans effort (un effort parapluie fait tout refuser)
        corps["reasoning"] = {"enabled": True}
        if inclure:
            corps["reasoning"]["include_reasoning"] = True
    elif inclure:
        # pas de raisonnement demande, mais on veut le voir s'il y en a
        corps["include_reasoning"] = True
    return corps


def appeler(modele: dict, prompt: str, leviers: dict, timeout: int = 180,
            reprises: int = 2) -> dict:
    """Un appel, avec toutes les mesures, et une reprise si la reponse est vide.

    Le proxy rend parfois une reponse VIDE avec finish_reason=stop : mesure a
    1 appel sur 8, sans raison apparente, tokens rapportes, aucune erreur. Ce
    n'est pas un resultat, c'est un echec du service — sans reprise, un huitieme
    du glossaire manque silencieusement. Comme un timeout, le seul remede est
    de reessayer.

    Les vraies erreurs (429, 400) ne sont pas reessayees : elles se
    reproduiraient, et on perdrait du temps pour rien.
    """
    dernier: dict = {"ok": False, "erreur": "aucun essai",
                     "latence": 0.0, "essais": 0, "vide": False}
    for essai in range(1, max(1, reprises) + 1):
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
            # 4xx/5xx : inutile de reessayer, ca se reproduirait
            return {"ok": False, "erreur": detail, "essais": essai,
                    "latence": round(time.time() - t0, 1), "vide": False}
        except Exception as e:  # noqa: BLE001
            dernier = {"ok": False,
                       "erreur": f"{type(e).__name__} apres {time.time()-t0:.0f}s",
                       "latence": round(time.time() - t0, 1), "essais": essai,
                       "vide": False}
            continue  # un timeout peut se resoudre

        msg = d["choices"][0]["message"]
        u = d.get("usage", {}) or {}
        det = u.get("completion_tokens_details", {}) or {}
        contenu = (msg.get("content") or "").strip()
        if not contenu and essai < reprises:
            continue  # reponse vide : on retente

        return {
            "ok": True,
            "sortie": contenu,
            "a_raisonne": bool(msg.get("reasoning")),
            "latence": round(time.time() - t0, 1),
            "tok_entree": u.get("prompt_tokens"),
            "tok_sortie": u.get("completion_tokens"),
            "tok_raisonnement": det.get("reasoning_tokens"),
            "finish": d["choices"][0].get("finish_reason"),
            "tronce": d["choices"][0].get("finish_reason") == "length",
            "essais": essai,
            # une reponse vide est un echec du service, pas un resultat vide
            "vide": not contenu,
        }

    return dernier


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

def _page_de_test(source: Path = SOURCE, projet: str | None = None) -> tuple[str, str, str]:
    """Une vraie page d'un vrai PDF, et son nom.

    Deux sources au choix :
      - source=None : un texte de repli, hors de tout projet
      - source=<dossier> + projet=<nom> : une page d'un projet precis

    Le choix est explicite parce qu'il change ce qu'on mesure : un glossaire
    sur une feuille de personnage n'a rien a voir avec un glossaire sur le
    roman. Sans le dire, on compare toujours le meme texte.
    """
    dossier = source
    if projet:
        # SOURCE = Projets/<nom>/source : le dossier des projets est deux
        # niveaux au-dessus, pas un
        racine = SOURCE.parent.parent if SOURCE.name == "source" else SOURCE.parent
        dossier = racine / projet / "source"
        if not dossier.is_dir():
            raise ValueError(f"projet sans dossier source : {dossier}")

    if dossier and dossier.is_dir():
        pdfs = sorted(p for p in dossier.glob("*.pdf") if not p.name.startswith("~$"))
        if pdfs:
            pdf = pdfs[0]
            doc = pymupdf.open(str(pdf))
            txt = doc[0].get_text()
            doc.close()
            return txt, pdf.name, "1"

    # repli hors projet : du texte qui ressemble a une page de jeu
    return (
        "The Lowland Wastes hold many secrets. Cloud Empress watches from the "
        "ridge, counting the smoke of the last convoy. The tithing altar still "
        "stands, though no one has paid tribute in years.\n"
        "Mark all party successes (S) and failures (F) chronologically below.",
        "repli.txt", "1")


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


def _score(scenario: str, n: dict) -> dict:
    """Une note sur 100, calculable sans lire le texte.

    Elle ne dit pas si la traduction est belle : elle dit si elle est
    UTILISABLE. Un score de 100 ne garantit rien sur le style, mais un score
    bas garantit un probleme. Pour trancher, on lit la reponse brute.

    Chaque critere a un poids : ce qui casse la production pese plus que ce qui
    degrade.
    """
    criteres: list[tuple[str, bool, int]] = [
        # ce qui casse partout : poids fort
        ("pas tronque", not n.get("tronce"), 30),
        ("pas de reflexion qui fuit", not n.get("reflexion_fuite"), 10),
    ]

    if scenario == "traduction":
        criteres.append(("traduit (pas identique)", not n.get("identique"), 25))
        # une traduction FR fait ~0,8 a 1,8 fois la source ; hors de cette
        # plage, c'est qu'il a tronque ou surrounds
        ratio = n.get("ratio")
        criteres.append(("longueur plausible",
                         ratio is not None and 0.5 <= ratio <= 3.0, 10))
        bal = n.get("balises")
        if bal and bal != "n/a":
            ok, total = bal.split("/")
            criteres.append((f"balises {bal}", int(ok) == int(total), 25))
        else:
            criteres.append(("balises conservees", True, 25))  # rien a perdre

    elif scenario == "glossaire":
        criteres.append(("des termes extraits", (n.get("termes") or 0) > 0, 40))
        criteres.append(("JSON exploitable", bool(n.get("exploitable")), 20))

    elif scenario == "polices":
        criteres.append(("JSON valide", bool(n.get("exploitable")), 40))
        criteres.append(("des polices vues", (n.get("termes") or 0) > 0, 20))

    else:  # contexte
        criteres.append(("un resume produit", (n.get("longueur") or 0) > 20, 40))
        criteres.append(("texte exploitable", bool(n.get("exploitable")), 20))

    total = sum(p for _, _, p in criteres)
    obtenu = sum(p for _, ok, p in criteres if ok)
    return {
        "score": round(100 * obtenu / total) if total else 0,
        "criteres": {nom: ok for nom, ok, _ in criteres},
    }


def _repetitions(mesures: list[dict]) -> dict:
    """Moyenne, min, max et ecart-type d'une serie de mesures.

    L'ecart-type est ce qui distingue un modele stable d'un modele chanceux :
    5s en moyenne avec un ecart de 4s ne vaut pas 5s avec un ecart de 1s.
    """
    def _serie(cle: str) -> list[float]:
        return [m[cle] for m in mesures if m.get(cle) is not None]

    out: dict = {}
    for cle, nom in (("latence", "lat"), ("termes", "termes")):
        v = _serie(cle)
        if not v:
            out[nom] = {"moy": None, "min": None, "max": None, "ecart": None}
            continue
        out[nom] = {
            "moy": round(statistics.mean(v), 1),
            "min": round(min(v), 1),
            "max": round(max(v), 1),
            # l'ecart-type n'a de sens qu'a partir de 2 mesures
            "ecart": round(statistics.stdev(v), 1) if len(v) > 1 else 0.0,
        }
    return out


def banc_charge(modele_id: str, niveaux: list[int], leviers: dict,
                timeout: int = 180, source: Path = SOURCE,
                projet: str | None = None, progres=None) -> dict:
    """La charge, presentee comme un scenario, avec un score.

    On mesure jusqu'ou le modele encaisse le parallele, et on note :
      100  = aucun palier n'a perdu d'appel
      70   = le premier palier qui casse, au-dela c'est inutile
      40   = ca casse des le premier

    Le score est ce qu'on compare entre modeles : 6 agents tenus a 100 vaut
    mieux que 24 agents tenus a 70.
    """
    cat = {m["id"]: m for m in catalogue()}
    modele = cat.get(modele_id)
    if not modele:
        return {"ok": False, "erreur": f"modele inconnu : {modele_id}"}

    lignes = []
    for n in niveaux:
        r = mesurer_charge(modele, n, max(n, 3), leviers, timeout, source)
        lignes.append(r)
        if progres:
            progres({"fait": len(lignes), "total": len(niveaux), "etape": "charge"})
        if r["nb_ok"] < r["blocs"]:
            break

    tenu = max((r["n_agents"] for r in lignes if r["nb_ok"] == r["blocs"]), default=0)
    total = lignes[-1]["n_agents"] if lignes else 0
    if total and tenu == total:
        score = 100
    elif tenu == 0:
        score = 40          # ca casse des le premier palier
    else:
        score = 70          # ca tient un moment, puis casse

    # la latence, c'est celle du DERNIER palier tenu : c'est le debit qu'on
    # aurait reellement en production
    tenues = [r for r in lignes if r["nb_ok"] == r["blocs"]]
    lat = tenues[-1]["total_s"] if tenues else (lignes[0]["total_s"] if lignes else None)

    return {
        "scenario": "charge",
        "ok": True,
        "latence": lat,
        "score": score,
        "criteres": {
            "aucun palier casse": tenu == total,
            "au moins 4 agents": tenu >= 4,
            "aucune troncature": all(not r["tronques"] for r in lignes),
        },
        "niveaux": lignes,
        "tenu": tenu,
        "niveaux_testes": total,
        "termes": sum(r["termes"] for r in tenues),
        "stats": {"lat": {"moy": lat, "min": None, "max": None,
                          "ecart": max((r["total_s"] for r in tenues), default=0)
                          - min((r["total_s"] for r in tenues), default=0)}},
        "repetitions": 1,
        "sortie_brute": "",
        "erreurs": sorted({e[:70] for r in lignes for e in r["erreurs"]})[:3],
    }


# ---------------------------------------------------------------- execution

def mesurer(modele: dict, scenario: str, leviers: dict, texte: str,
            doc: str, page: str, timeout: int = 180,
            repetitions: int = 1) -> dict:
    """Un modele, un scenario, mesure.

    Avec repetitions > 1, on repete et on garde la MOYENNE des mesures, plus
    min/max/ecart-type et la reponse brute d'un run. La repetition sert a deux
    choses : voir la stabilite (l'ecart-type), et ne pas juger un modele sur un
    appel qui a eu de la chance ou de la malchance.
    """
    prompt = prompt_scenario(scenario, texte, doc, page)

    if repetitions <= 1:
        r = appeler(modele, prompt, leviers, timeout)
        n = noter(scenario, texte, r)
        n.update(scenario=scenario, modele=modele["id"], prompt_car=len(prompt),
                 repetitions=1)
        n["score"] = _score(scenario, n)["score"]
        n["criteres"] = _score(scenario, n)["criteres"]
        n["sortie_brute"] = r.get("sortie", "")[:4000] if r.get("ok") else ""
        return n

    mesures: list[dict] = []
    brut = ""
    for _ in range(repetitions):
        r = appeler(modele, prompt, leviers, timeout)
        m = noter(scenario, texte, r)
        mesures.append(m)
        # on garde la premiere reponse exploitable : c'est ce que tu lis
        if not brut and m.get("exploitable"):
            brut = r.get("sortie", "")[:4000]

    reussies = [m for m in mesures if m.get("ok")]
    # on resume sur la MEILLEURE mesure exploitable, sinon sur la premiere :
    # une moyenne de mesures non exploitables ne veut rien dire
    base = next((m for m in mesures if m.get("exploitable")), mesures[0])

    n = dict(base)
    n.update(scenario=scenario, modele=modele["id"], prompt_car=len(prompt),
             repetitions=repetitions, reussis=len(reussies))
    n["mesures"] = mesures
    n["stats"] = _repetitions(reussies or mesures)
    n["lat_moy"] = n["stats"]["lat"]["moy"]
    n["score"] = _score(scenario, n)["score"]
    n["criteres"] = _score(scenario, n)["criteres"]
    n["sortie_brute"] = brut
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
           timeout: int = 180, repetitions: int = 1,
           source: Path = SOURCE, projet: str | None = None,
           avec_charge: bool = False, charge_niveaux: list[int] | None = None,
           progres=None) -> dict:
    """Tous les modeles, tous les scenarios, en parallele.

    avec_charge ajoute la CHARGE comme un scenario de plus : elle apparait
    alors dans le comparatif, avec son propre score. C'est ce qu'on veut quand
    on compare des modeles — savoir combien d'agents chacun encaisse.
    """
    texte, doc, page = _page_de_test(source, projet)
    tasks = [(m, s) for m in modeles for s in scenarios]
    brut: dict[tuple[str, str], dict] = {}

    # 6 workers : au-dela, on se dispute le service (mesure par le Banc charge)
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {
            ex.submit(mesurer, m, s, leviers, texte, doc, page, timeout,
                      repetitions): (m["id"], s)
            for m, s in tasks
        }
        for f in as_completed(futs):
            m, s = futs[f]
            brut[(m, s)] = f.result()
            if progres:
                # fait/total sur les scenarios : une barre qui avance
                progres({"fait": len(brut), "total": len(futs), "etape": s})

    noms_all = list(scenarios) + (["charge"] if avec_charge else [])
    par_modele = []
    for m in modeles:
        reps = [brut.get((m["id"], s), {}) for s in scenarios]

        # la charge, si demandee : un pseudo-scenario avec les memes cles, pour
        # qu'elle se trie et s'affiche comme les autres
        if avec_charge:
            r = banc_charge(m["id"], charge_niveaux or [1, 2, 4, 8], leviers,
                            timeout, source, projet, progres)
            reps.append(r)
        scores = [r["score"] for r in reps if r.get("score") is not None]
        ligne = {"modele": m["id"], "nom": m.get("nom", ""),
                 "effort": leviers.get("effort") or "aucun",
                 # la note globale : moyenne des scenarios. C'est ce qui
                 # permet de classer d'un coup d'oeil.
                 "score": round(statistics.mean(scores)) if scores else 0,
                 **_resume(reps)}
        for s, r in zip(noms_all, reps):
            ligne[s] = r
        par_modele.append(ligne)

    noms = list(scenarios) + (["charge"] if avec_charge else [])
    return {
        "maj": datetime.now().isoformat(timespec="seconds"),
        "leviers": leviers,
        "repetitions": repetitions,
        "scenarios": noms,
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


# ---------------------------------------------------------------- charge
#
# La question : combien d'appels simultanes le modele encaisse-t-il ?
# C'est celle qu'on se pose avant de lancer 40 blocs de glossaire en
# parallele, et le Banc doit y repondre tout seul.

def blocs_de_charge(n: int, source: Path = SOURCE) -> list[dict]:
    """n blocs de travail, pris dans les vrais PDF du projet.

    Hors projet (pas de PDF, ou dossier vide), on fabrique des blocs a partir
    d'un texte de repli. Le repli doit en produire autant que demande, sinon le
    mode charge ment sur le nombre d'agents qu'il teste.
    """
    if n <= 0:
        return []

    if source.is_dir():
        taches: list[dict] = []
        for pdf in sorted(source.glob("*.pdf")):
            doc = pymupdf.open(str(pdf))
            for i in range(min(doc.page_count, 4)):
                taches += _agents.planifier(
                    [(pdf.name, i + 1, str(doc[i].get_text()))])
            doc.close()
            if len(taches) >= n:
                break
        if len(taches) >= n:
            return taches[:n]

    # repli : du texte repeté, decoupe par le meme planificateur que la prod
    base = ("The Lowland Wastes hold many secrets. Cloud Empress watches from "
            "the ridge, counting the smoke. ") * 60
    return _agents.planifier([("repli.pdf", 1, base)])[:n]


def mesurer_charge(modele: dict, n_agents: int, nb_blocs: int,
                   leviers: dict, timeout: int = 180,
                   source: Path = SOURCE) -> dict:
    """nb_blocs blocs, n_agents en meme temps.

    On mesure le temps total du lot, les reussites, et le debit : c'est ce qui
    dit si paralleliser sert vraiment ou si on perd du temps en se disputant
    le service.
    """
    blocs = blocs_de_charge(nb_blocs, source)
    prompts = [_agents.prompt_extraction(b["texte"]) for b in blocs]

    t0 = time.time()
    resultats: list[dict] = []
    # as_completed : un banc long affiche au fur et a mesure, sinon rien ne
    # bouge pendant des minutes
    with ThreadPoolExecutor(max_workers=n_agents) as ex:
        futs = [ex.submit(appeler, modele, p, leviers, timeout) for p in prompts]
        for f in as_completed(futs):
            r = f.result()
            r["exploitable"] = (not r.get("tronce")) and bool(
                _agents.lire_reponse(r.get("sortie", "")))
            resultats.append(r)
    total = time.time() - t0

    ok = [r for r in resultats if r.get("ok")]
    lats = sorted(r["latence"] for r in ok)
    termes = sum(len(_agents.lire_reponse(r.get("sortie", ""))) for r in ok)

    # le gain compare au sequentiel : si paralleliser n'ameliore rien, ca veut
    # dire qu'on se dispute le service, pas qu'on gagne du temps
    lineaire = sum(lats)
    return {
        "n_agents": n_agents,
        "blocs": len(blocs),
        "reussis": f"{len(ok)}/{len(resultats)}",
        "nb_ok": len(ok),
        "total_s": round(total, 1),
        "lat_moy": round(statistics.mean(lats), 1) if lats else None,
        "lat_min": round(min(lats), 1) if lats else None,
        "lat_max": round(max(lats), 1) if lats else None,
        # 1.0 = aucun gain, 3.0 = trois fois plus rapide que du sequentiel
        "gain": round(lineaire / total, 2) if total else None,
        "termes": termes,
        "tronques": sum(1 for r in ok if r.get("tronce")),
        "erreurs": sorted({e[:70] for r in resultats
                           for e in [r.get("erreur")] if e})[:4],
    }


def lancer_charge(modele: dict, niveaux: list[int], leviers: dict,
                  timeout: int = 180, source: Path = SOURCE,
                  progres=None) -> dict:
    """Monte la charge, du plus petit au plus grand.

    On s'arrete au premier niveau qui casse : la reponse utile d'un modele
    lent, c'est savoir ou est la limite, pas la repousser.
    """
    lignes = []
    for n in niveaux:
        # autant de blocs que d'agents : sinon on mesure la queue, pas la pointe
        r = mesurer_charge(modele, n, max(n, 3), leviers, timeout, source)
        lignes.append(r)
        if progres:
            progres(r)
        # un niveau qui perd un appel, les suivants seront moins bons
        if r["nb_ok"] < r["blocs"]:
            break

    return {
        "maj": datetime.now().isoformat(timespec="seconds"),
        "modele": modele["id"],
        "leviers": leviers,
        "niveaux": lignes,
        # le plus grand nombre d'agents qui a tenu sans perdre d'appel
        "tenu": max((r["n_agents"] for r in lignes if r["nb_ok"] == r["blocs"]),
                    default=0),
    }


def charger_charge() -> dict:
    if CHARGE.is_file():
        try:
            return json.loads(CHARGE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"niveaux": [], "maj": None}


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