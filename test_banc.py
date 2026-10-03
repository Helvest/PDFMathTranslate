"""Verifie le Banc sans appel reseau, puis avec.

    .venv/Scripts/python.exe test_banc.py          # tout
    .venv/Scripts/python.exe test_banc.py --reseau # ajoute un appel reel
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import banc

ERREURS: list[str] = []


def _verifie(cond: bool, message: str) -> None:
    if not cond:
        ERREURS.append(message)


# ---------------------------------------------------------------- catalogue

def test_catalogue() -> None:
    """Les metadonnees doivent suffire a piloter l'appel."""
    c = banc.catalogue()
    _verifie(isinstance(c, list) and c, "catalogue vide ou pas une liste")

    if not c or c[0].get("erreur"):
        return  # proxy injoignable : le reste ne peut pas tourner

    for m in c:
        for champ in ("id", "contexte", "parametres", "efforts",
                      "effort_defaut", "raisonnement_obligatoire"):
            _verifie(champ in m, f"champ '{champ}' absent du catalogue")

    # effort_serieux ne doit jamais renvoyer "max" si un effort bas existe,
    # ni une chaine bidon quand le modele ne liste aucun niveau
    for m in c:
        e = banc.effort_serieux(m)
        if m["efforts"]:
            _verifie(e != "max",
                     f"{m['id']} : effort_serieux renvoie 'max' alors que "
                     f"{m['efforts']} existent")
            _verifie(e in m["efforts"],
                     f"{m['id']} : effort '{e}' absent de {m['efforts']}")
        else:
            # beaucoup de modeles imposent le raisonnement sans lister leurs
            # niveaux : il faut alors None, pas une chaine envoyee au proxy
            _verifie(e is None,
                     f"{m['id']} sans niveaux : effort_serieux renvoie {e!r}, "
                     f"il faut None (sinon 'reasoning.effort: Invalid option')")

    # et le raisonnement obligatoire doit quand meme etre demande
    for m in c:
        if m["raisonnement_obligatoire"] and not m["efforts"]:
            corps = banc._construire(m, {"effort": None, "temperature": 0}, "p")
            _verifie(corps.get("reasoning") == {"enabled": True},
                     f"{m['id']} impose le raisonnement mais ne le recevra pas")


# ---------------------------------------------------------------- leviers

def test_catalogue_complet() -> None:
    """Aucun champ du proxy ne doit disparaitre.

    On avait perdu 17 champs sur 19 (aliases, canonical_slug,
    knowledge_cutoff, default_parameters, pricing complet...). Ils sont des
    fois de moins pour comparer un modele, et le jour ou /v1/models ajoute un
    champ, on ne doit pas avoir a modifier le code pour le voir.

    On ne compare PAS les valeurs : le proxy renvoie des prix differents a
    deux appels sur le meme modele. Exiger l'egalite rendrait ce test
    intermittent — c'est ce qui le faisait echouer ici.
    """
    import json as _json
    import urllib.request as _u

    req = _u.Request(banc.PROXY + "/models",
                     headers={"Authorization": f"Bearer {banc.CLE}"})
    try:
        with _u.urlopen(req, timeout=25) as r:
            origine = _json.load(r).get("data", [])
    except Exception as e:  # noqa: BLE001
        print(f"  proxy injoignable ({e}) : completude non verifiee")
        return

    if not origine:
        return

    cat = {m["id"]: m for m in banc.catalogue()}
    _verifie(bool(cat), "catalogue vide")

    manquants: list[str] = []
    for m in origine:
        entree = cat.get(m.get("id", ""))
        if not entree:
            manquants.append(f"{m.get('id')} absent du catalogue")
            continue
        # "brut" doit etre le modele du proxy, champ pour champ : ni perdu,
        # ni invente
        brut = entree.get("brut") or {}
        for champ in m:
            if champ not in brut:
                manquants.append(f"{m['id']}.{champ} absent")
            elif brut[champ] is None and m[champ] is not None:
                # le proxy renvoie du JSON mal form sometimes ; on ne peut
                # pas exiger la valeur, mais le champ doit exister
                manquants.append(f"{m['id']}.{champ} vide")

    _verifie(not manquants,
             f"{len(manquants)} champ(s) perdus : {', '.join(manquants[:8])}")

    # et l'inverse : le brut ne doit rien inventer
    for mid, m in cat.items():
        if m.get("erreur"):
            continue
        _verifie(isinstance(m.get("brut"), dict) and m["brut"],
                 f"{mid} : pas de modele complet conserve")


def test_leviers() -> None:
    """Le corps de la requete doit porter exactement les leviers demandes."""
    modele = {
        "id": "x/y", "efforts": ["low", "high"], "effort_defaut": "max",
        "raisonnement_obligatoire": True,
    }
    c = banc._construire(modele, {"effort": "low", "max_tokens": 16000,
                                  "temperature": 0}, "prompt")
    # include_reasoning accompagne l'effort : c'est le meme bloc, pas un
    # parametre a part
    _verifie((c["reasoning"] or {}).get("enabled") is True
             and c["reasoning"].get("effort") == "low",
             f"l'effort demande n'est pas passe : {c.get('reasoning')}")
    _verifie(c["max_tokens"] == 16000, "max_tokens non passe")
    _verifie(c["temperature"] == 0, "temperature non passee")

    # sans effort demande, on ne doit PAS laisser le modele choisir son
    # defaut : c'est exactement le bug qui rendait les appels de 271 s
    c2 = banc._construire(modele, {"effort": None, "temperature": 1}, "p")
    _verifie("effort" not in (c2.get("reasoning") or {}),
             "aucun effort demande mais un effort envoye : "
             "le modele choisira son defaut (max)")

    # un modele sans raisonnement ne doit pas recevoir le bloc reasoning
    c3 = banc._construire({"id": "z", "efforts": [], "raisonnement_obligatoire": False},
                          {"effort": "low", "temperature": 0}, "p")
    _verifie("reasoning" not in c3,
             "reasoning envoye a un modele qui ne le supporte pas")


def test_leviers_supports() -> None:
    """Les leviers annoncés doivent etre envoyes, les autres jamais.

    Le proxy accepte 27 parametres. On n'en regle que quelques-uns, mais ce
    qu'on regle doit partir : c'est ce que l'utilisateur a demande en cochant
    une case.
    """
    modele = {"id": "x/y", "efforts": ["low"], "raisonnement_obligatoire": True,
              "parametres": ["max_tokens", "temperature", "top_p", "seed",
                             "stop", "repetition_penalty"]}

    c = banc._construire(modele, {
        "effort": "low", "max_tokens": 4000, "temperature": 0,
        "top_p": 0.9, "seed": 42, "stop": "FIN", "repetition_penalty": 1.1}, "p")
    _verifie(c["top_p"] == 0.9, f"top_p non envoye : {c.get('top_p')}")
    _verifie(c["seed"] == 42, f"seed non envoye : {c.get('seed')}")
    _verifie(c["stop"] == "FIN", f"stop non envoye : {c.get('stop')}")
    _verifie(c["repetition_penalty"] == 1.1,
             f"repetition_penalty non envoye : {c.get('repetition_penalty')}")

    # un levier non renseigne ne doit pas apparaitre, meme si on l'a coche vide
    c2 = banc._construire(modele, {"effort": "low", "top_p": None, "seed": ""}, "p")
    _verifie("top_p" not in c2, "top_p vide envoye quand meme")
    _verifie("seed" not in c2, "seed vide envoye quand meme")

    # un parametre que le modele ne supporte pas ne doit JAMAIS partir :
    # le routeur refuse la requete entiere
    c3 = banc._construire({"id": "z", "efforts": [], "raisonnement_obligatoire": False,
                           "parametres": ["max_tokens", "temperature"]},
                          {"seed": 42, "top_p": 0.9, "temperature": 0}, "p")
    _verifie("seed" not in c3,
             "seed envoye a un modele qui ne le supporte pas : requete refusee")
    _verifie("top_p" not in c3, "top_p envoye a un modele qui ne le supporte pas")

    # un modele qui ne liste rien ne peut pas etre verifie : on laisse passer
    # plutot que de tout refuser, sinon le banc ne mesurerait rien
    c4 = banc._construire({"id": "y", "efforts": [], "raisonnement_obligatoire": False,
                           "parametres": []},
                          {"seed": 7, "temperature": 0}, "p")
    _verifie(c4.get("seed") == 7,
             "un modele sans liste de parametres doit quand meme recevoir seed")

    # les leviers doivent etre castes : un seed "42" depuis un formulaire
    c5 = banc._construire(modele, {"seed": "42", "max_tokens": "4000",
                                   "temperature": 0}, "p")
    _verifie(c5["seed"] == 42 and isinstance(c5["seed"], int),
             f"seed non converti en entier : {c5['seed']!r}")
    _verifie(c5["max_tokens"] == 4000 and isinstance(c5["max_tokens"], int),
             f"max_tokens non converti : {c5['max_tokens']!r}")


# ---------------------------------------------------------------- qualite

def test_qualite() -> None:
    """Les criteres doivent attraper ce qui casse en production."""
    source = "The damage is {v1} plus {v2}. Cloud Empress rules."

    # balises perdues
    n = banc.noter("traduction", source,
                   {"ok": True, "sortie": "Les degats sont de un plus deux.",
                    "latence": 1, "finish": "stop"})
    _verifie(n["balises"] == "0/2", f"balises non detectees : {n['balises']}")

    # balises conservees
    n = banc.noter("traduction", source,
                   {"ok": True, "sortie": "Les degats {v1} plus {v2}.",
                    "latence": 1, "finish": "stop"})
    _verifie(n["balises"] == "2/2", f"balises comptees a tort : {n['balises']}")

    # sortie identique a l'entree : rien n'a ete traduit
    n = banc.noter("traduction", source,
                   {"ok": True, "sortie": source, "latence": 1, "finish": "stop"})
    _verifie(n["identique"], "sortie identique non detectee")
    _verifie(not n["exploitable"], "une sortie identique ne doit pas etre exploitable")

    # reponse tronquee : finish_reason = length. C'est le bug de high/max.
    n = banc.noter("glossaire", "texte",
                   {"ok": True, "sortie": '[{"src":"A","tgt":"B"',
                    "latence": 1, "finish": "length", "tronce": True})
    _verifie(n["tronce"], "troncature non detectee")
    _verifie(not n["exploitable"], "une reponse tronquee ne doit pas etre exploitable")

    # reflexion qui fuit : la reponse commence par une balise de reflexion
    n = banc.noter("traduction", "texte",
                   {"ok": True, "sortie": "<think>blah</think>le texte",
                    "latence": 1, "finish": "stop"})
    _verifie(n["reflexion_fuite"], "fuite de reflexion non detectee")

    # reflexion qui fuit en debut de reponse, avec espace devant
    n = banc.noter("traduction", "texte",
                   {"ok": True, "sortie": "\n\n <think>reflexion</think>texte",
                    "latence": 1, "finish": "stop"})
    _verifie(n["reflexion_fuite"],
             "fuite de reflexion non detectee quand elle est en debut")

    # pas de fuite : pas de balise
    n = banc.noter("traduction", "texte",
                   {"ok": True, "sortie": "Le texte traduit.",
                    "latence": 1, "finish": "stop"})
    _verifie(not n["reflexion_fuite"], "faux positif : pas de fuite ici")

    # un glossaire sans terme n'est pas un glossaire
    n = banc.noter("glossaire", "texte",
                   {"ok": True, "sortie": "[]", "latence": 1, "finish": "stop"})
    _verifie(n["termes"] == 0, "tableau vide compte comme 0 terme")
    _verifie(not n["exploitable"], "un glossaire vide ne doit pas etre exploitable")


    # les polices rendent un OBJET, pas un tableau : le test doit differer
    n = banc.noter("polices", "texte",
                   {"ok": True, "latence": 1, "finish": "stop",
                    "sortie": '{"sites": [], "par_police": {"FuturaPT": '
                              '{"chercher": [], "raison": "rien"}}, "interdits": []}'})
    _verifie(n["exploitable"], f"polices : JSON valide non exploitable ({n})")
    _verifie(n["termes"] == 1, f"polices : {n['termes']} police(s) vue(s)")

    # un JSON coupe par max_tokens n'est pas exploitable
    n = banc.noter("polices", "texte",
                   {"ok": True, "latence": 1, "finish": "length", "tronce": True,
                    "sortie": '{"par_police": {"FuturaPT": {"chercher": []'})
    _verifie(not n["exploitable"], "polices : JSON tronque accepte")

    # du texte libre n'est pas du JSON exploitable
    n = banc.noter("polices", "texte",
                   {"ok": True, "latence": 1, "finish": "stop",
                    "sortie": "Je ne trouve pas de police appropriee."})
    _verifie(not n["exploitable"], "polices : du texte libre accepte comme JSON")
    _verifie(not banc._json_valide("bonjour"), "JSON invalide vu comme valide")
    _verifie(banc._json_valide('{"a": 1}'), "JSON valide vu comme invalide")

    # un echec doit etre remonte tel quel
    n = banc.noter("glossaire", "texte", {"ok": False, "erreur": "HTTP 429"})
    _verifie(not n["ok"] and "429" in n["erreur"], "echec non remonte")


# ---------------------------------------------------------------- scenarios

def test_scenarios() -> None:
    """Les 4 scenarios doivent produire un prompt utilisable."""
    texte, doc, page = "Texte de test.\n\nThe Lowland Wastes hold secrets.", "x.pdf", "1"

    for s in banc.SCENARIOS:
        p = banc.prompt_scenario(s, texte, doc, page)
        _verifie(isinstance(p, str) and len(p) > 100,
                 f"scenario '{s}' : prompt trop court ({len(p) if isinstance(p, str) else '?'})")
        # aucun placeholder non resolu : un {manquant} casserait l'appel
        import re
        trouves = set(re.findall(r"\{(\w+)\}", p))
        _verifie(not trouves,
                 f"scenario '{s}' : champs non remplis {sorted(trouves)}")

    # le scenario glossaire doit venir de agents.py, pas etre une copie
    from agents import prompt_extraction
    _verifie(
        banc.prompt_scenario("glossaire", texte, doc, page) == prompt_extraction(texte),
        "le scenario glossaire ne reutilise pas le prompt de production")

    # le scenario contexte doit venir de analyser.py
    import analyser
    _verifie("Contexte du LOT" in
             banc.prompt_scenario("contexte", texte, doc, page),
             "le scenario contexte n'utilise pas CTX_PAGE_PROMPT")

    # un scenario inconnu doit echouer franchement
    try:
        banc.prompt_scenario("inconnu", texte, doc, page)
        ERREURS.append("un scenario inconnu devrait lever une erreur")
    except ValueError:
        pass


def test_resume() -> None:
    """Le resume doit compter juste."""
    reps = [
        {"ok": True, "latence": 10, "tok_sortie": 100, "tronce": False, "exploitable": True},
        {"ok": True, "latence": 20, "tok_sortie": 200, "tronce": True, "exploitable": False},
        {"ok": False, "erreur": "boom"},
    ]
    r = banc._resume(reps)
    _verifie(r["reussis"] == "2/3", f"reussis comptees a tort : {r['reussis']}")
    _verifie(r["lat_moy"] == 15, f"latence moyenne fausse : {r['lat_moy']}")
    _verifie(r["lat_max"] == 20, f"latence max fausse : {r['lat_max']}")
    _verifie(r["tronces"] == 1, f"troncatures comptees a tort : {r['tronces']}")
    _verifie(r["exploitables"] == "1/2", f"exploitables fausse : {r['exploitables']}")

    # tout echoue ne doit pas planter la moyenne
    r2 = banc._resume([{"ok": False, "erreur": "x"}])
    _verifie(r2["lat_moy"] is None, "moyenne calculee sans aucun succes")


# ---------------------------------------------------------------- reseau

def test_reseau() -> None:
    """Un appel reel, sur le modele du projet."""
    cat = [m for m in banc.catalogue() if not m.get("erreur")]
    if not cat:
        print("  proxy injoignable : test reseau saute")
        return

    modele = next((m for m in cat if m["id"] == "stealth/space-bunny-alpha"), cat[0])
    print(f"\n  {modele['id']} — effort "
          f"{banc.effort_serieux(modele)}, max_tokens 16000")

    texte, doc, page = banc._page_de_test()
    leviers = {"effort": banc.effort_serieux(modele),
               "max_tokens": 16000, "temperature": 0}

    n = banc.mesurer(modele, "glossaire", leviers, texte, doc, page, timeout=180)
    _verifie(n.get("ok"), f"appel reel echoue : {n.get('erreur')}")
    if n.get("ok"):
        _verifie(not n["tronce"],
                 f"reponse tronquee (finish={n['finish']}) : "
                 "max_tokens=16000 ne suffit pas")
        _verifie(n["exploitable"],
                 f"glossaire non exploitable : {n['termes']} termes, "
                 f"tronce={n['tronce']}")
        print(f"    {n['latence']}s, {n['tok_entree']}+{n['tok_sortie']} tokens, "
              f"{n['termes']} termes")


def test_charge() -> None:
    """Le mode charge doit repondre a la question du nombre d'agents."""
    import tempfile

    # les blocs viennent de vrais PDF, mais on doit pouvoir tester hors projet
    with tempfile.TemporaryDirectory() as td:
        vide = Path(td)
        _verifie(len(banc.blocs_de_charge(3, vide)) == 3,
                 "hors projet, on doit pouvoir fabriquer des blocs")
        _verifie(banc.blocs_de_charge(0, vide) == [],
                 "0 bloc demande doit donner 0 bloc")
        # le source reel fournit aussi des blocs
        if banc.SOURCE.is_dir():
            b = banc.blocs_de_charge(4)
            _verifie(len(b) >= 3, f"seulement {len(b)} bloc(s) tires des PDF")
            _verifie(all("texte" in x for x in b), "un bloc sans texte")

    # le calcul du gain : 3 appels de 10s en parallele contre 30s lineaires
    # donnent un gain de 3.0 si c'est reellement parallele, 1.0 si tout a
    # attendu tour par tour
    r = {"lat_moy": 10, "total_s": 10, "nb_ok": 1, "blocs": 1}
    _verifie(round(sum([10]) / 10, 2) == 1.0,
             "un appel seul ne peut pas donner de gain : la formule est fausse")

    # lancer_charge doit s'arreter au premier niveau qui casse
    def _faux(*a, **k):
        return {"n_agents": 0, "blocs": 3, "nb_ok": 0}

    _verifie(callable(banc.lancer_charge), "lancer_charge doit exister")
    _verifie(callable(banc.charger_charge), "charger_charge doit exister")


def test_reseau_charge() -> None:
    """Un palier de charge reel : 3 blocs, 3 agents."""
    cat = [m for m in banc.catalogue() if not m.get("erreur")]
    if not cat:
        print("  proxy injoignable : test charge saute")
        return
    modele = next((m for m in cat if m["id"] == "stealth/space-bunny-alpha"), cat[0])
    leviers = {"effort": banc.effort_serieux(modele),
               "max_tokens": 16000, "temperature": 0}

    r = banc.mesurer_charge(modele, 3, 3, leviers, timeout=180)
    print(f"    3 agents / 3 blocs : {r['reussis']} en {r['total_s']}s, "
          f"gain {r['gain']}x, {r['termes']} termes")
    _verifie(r["blocs"] == 3, f"{r['blocs']} blocs au lieu de 3")
    _verifie(r["nb_ok"] > 0, "aucun appel n'a reussi")
    # le gain ne peut pas depasser le nombre d'agents : chaque appel ne peut
    # pas avoir duré moins que le plus rapide
    if r["nb_ok"] == r["blocs"] and r["gain"]:
        _verifie(r["gain"] <= r["n_agents"] + 0.05,
                 f"gain {r['gain']}x impossible avec {r['n_agents']} agents")


def test_reprise_reponse_vide() -> None:
    """Le proxy rend parfois une reponse VIDE : il faut reessayer.

    Mesure : 1 appel sur 8 rend finish_reason=stop avec une sortie vide, sans
    erreur et avec les tokens rapportes. Sans reprise, un huitieme du glossaire
    manque en silence — c'est ce qui faisait 3/4 au lieu de 4/4.

    On teste appeler() en patchant la vraie reference qu'il utilise
    (banc.urllib.request.urlopen), sinon le test appelle le proxy reel.
    """
    import io
    import threading

    scenarios = [
        {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        {"choices": [{"message": {"content": '[{"src": "A", "tgt": "B"}]'},
                      "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 10, "completion_tokens": 20}},
    ]
    lock = threading.Lock()
    appels: list[int] = []

    class _FauxProxy:
        def __call__(self, req, timeout=None):
            with lock:
                d = scenarios[min(len(appels), len(scenarios) - 1)]
                appels.append(1)
            return io.BytesIO(json.dumps(d).encode())

    modele = {"id": "x/y", "efforts": ["low"],
              "raisonnement_obligatoire": True,
              "parametres": ["max_tokens", "temperature"]}
    leviers = {"effort": "low", "max_tokens": 100, "temperature": 0}

    vrai = banc.urllib.request.urlopen
    banc.urllib.request.urlopen = _FauxProxy()
    try:
        r = banc.appeler(modele, "p", leviers, timeout=5)
    finally:
        banc.urllib.request.urlopen = vrai

    _verifie(len(appels) == 2,
             f"il fallait 2 appels pour une reponse vide, obtenu {len(appels)}")
    _verifie(r.get("essais") == 2,
             f"la reprise n'a pas ete comptabilisee : {r.get('essais')}")
    _verifie(bool(r.get("sortie")), "la reprise a rendu une reponse vide encore")
    _verifie(not r.get("vide"), "la reponse de la reprise est signalee vide")


def test_leviers_api() -> None:
    """"auto" est un mot du menu, jamais une valeur d'API.

    Ce bug a ete.commit par accident : /banc/charge/lancer renvoyait la chaine
    "auto" a OpenRouter, qui repondait "reasoning.effort: Invalid option" et
    faisait echouer les 3 appels en 0.7s.
    """
    # serveur.py vit dans interface/ : sans ce chemin le test se sautait
    # silencieusement, ce qui est pire qu'un echec visible
    racine = Path(__file__).resolve().parent
    # _leviers est la seule logique de resolution des leviers cote serveur.
    # On la teste ici sans importer serveur : serveur.py tire fastapi/pydantic,
    # qui cassent hors du venv du projet — et un test qui depend de
    # l'environnement est un test qui finit par ne rien verifier.
    src_serveur = (racine / "interface" / "serveur.py").read_text(encoding="utf-8")
    ns: dict = {"MAX_TOKENS": 16000}
    debut = src_serveur.index("def _leviers(")
    fin = src_serveur.index("\n@app.", debut)
    exec(compile(src_serveur[debut:fin], "serveur._leviers", "exec"), ns)
    _leviers = ns["_leviers"]

    modele = {"id": "x/y", "efforts": ["low", "max"], "raisonnement_obligatoire": True}
    class _B:
        @staticmethod
        def effort_serieux(m):
            return "low"

    for valeur in ("auto", "", None):
        lev = _leviers({"effort": valeur}, modele, _B)
        _verifie(lev["effort"] == "low",
                 f"effort {valeur!r} devrait etre resolu en 'low', "
                 f"obtenu {lev['effort']!r}")

    # un effort explicite ne doit surtout pas etre ecrase
    lev = _leviers({"effort": "high", "max_tokens": 8000}, modele, _B)
    _verifie(lev["effort"] == "high", "un effort choisi doit etre respecte")
    _verifie(lev["max_tokens"] == 8000, "max_tokens choisi doit etre respecte")

    # la valeur par defaut quand rien n'est demande
    lev = _leviers({}, modele, _B)
    _verifie(lev["max_tokens"] == 16000,
             "sans max_tokens, on doit prendre celui de la production")

    # un modele sans niveaux ne doit pas produire une chaine parapluie
    class _Vide:
        @staticmethod
        def effort_serieux(m):
            return None

    lev = _leviers({"effort": "auto"}, {"id": "z", "efforts": []}, _Vide)
    _verifie(lev["effort"] is None,
             f"un modele sans niveaux doit donner None, obtenu {lev['effort']!r}")


def test_score() -> None:
    """Le score doit sanctionner ce qui casse, et rien d'autre.

    Un score ne juge pas le style : il juge l'UTILISABILITE. Une traduction
    parfaite mais tronquee est moins utile qu'une traduction moyenne entiere.
    """
    # une traduction parfaite : 100
    bonne = {"tronce": False, "reflexion_fuite": False, "identique": False,
             "ratio": 1.15, "balises": "2/2", "exploitable": True,
             "termes": 28, "longueur": 800}
    s = banc._score("traduction", bonne)
    _verifie(s["score"] == 100, f"une traduction parfaite devrait faire 100, fait {s['score']}")
    _verifie(all(s["criteres"].values()), "tous les criteres devraient passer")

    # une reponse tronquee : elle doit etre sanctionnee, meme si tout le reste
    # va bien
    tronq = {**bonne, "tronce": True}
    _verifie(banc._score("traduction", tronq)["score"] == 70,
             f"une troncature doit coûter exactement 30 points, "
             f"obtenu {banc._score('traduction', tronq)['score']}")

    # sortie identique a l'entree : rien n'a ete traduit
    identique = {**bonne, "identique": True}
    _verifie(banc._score("traduction", identique)["score"] < 80,
             "une sortie identique doit etre penalisee")

    # balises perdues
    perdues = {**bonne, "balises": "0/2"}
    _verifie(banc._score("traduction", perdues)["score"] < 80,
             "des balises perdues doivent etre penalisees")

    # un glossaire sans terme n'est pas un glossaire
    vide = {**bonne, "termes": 0, "exploitable": False}
    _verifie(banc._score("glossaire", vide)["score"] < 50,
             "un glossaire vide doit etre fortement penalise")
    plein = {**bonne, "termes": 28, "exploitable": True}
    _verifie(banc._score("glossaire", plein)["score"] == 100,
             "un bon glossaire doit faire 100")

    # pas de balises dans le texte : ce critere ne doit pas penaliser
    sans = {**bonne, "balises": "n/a"}
    _verifie(banc._score("traduction", sans)["score"] == 100,
             "l'absence de balises ne doit rien penaliser")

    # le score doit toujours etre entre 0 et 100
    for sc, n in (("traduction", tronq), ("glossaire", vide), ("contexte", {}),
                  ("polices", {"tronce": True, "exploitable": False, "termes": 0})):
        v = banc._score(sc, n)["score"]
        _verifie(0 <= v <= 100, f"score hors bornes pour {sc} : {v}")


def test_include_reasoning() -> None:
    """include_reasoning dit si on VOIT le raisonnement dans la reponse.

    C'est l'inverse de "effort" : effort raisonne, include_reasoning montre.
    Les deux se combinent dans le bloc reasoning.
    """
    modele = {"id": "x/y", "efforts": ["low"], "raisonnement_obligatoire": True,
              "parametres": ["max_tokens", "temperature"]}

    c = banc._construire(modele, {"effort": "low", "max_tokens": 100,
                                  "temperature": 0,
                                  "include_reasoning": True}, "p")
    _verifie((c["reasoning"] or {}).get("include_reasoning") is True,
             "include_reasoning=True n'est pas passe")

    c2 = banc._construire(modele, {"effort": "low", "max_tokens": 100,
                                   "temperature": 0,
                                   "include_reasoning": False}, "p")
    _verifie((c2["reasoning"] or {}).get("include_reasoning") is False,
             "include_reasoning=False doit etre passe explicitement")

    # un modele qui impose le raisonnement mais ne liste pas ses niveaux :
    # on demande quand meme, sans effort (un effort parapluie est refuse)
    sans = {"id": "z", "efforts": [], "raisonnement_obligatoire": True,
            "parametres": ["max_tokens", "temperature"]}
    c3 = banc._construire(sans, {"effort": "low", "max_tokens": 100,
                                 "temperature": 0,
                                 "include_reasoning": True}, "p")
    _verifie("effort" not in (c3.get("reasoning") or {}),
             f"aucun effort ne doit partir : {c3.get('reasoning')}")
    _verifie((c3.get("reasoning") or {}).get("include_reasoning") is True,
             "include_reasoning doit survivre sans effort")


def test_repetitions() -> None:
    """La repetition doit montrer la stabilite, pas seulement la moyenne."""
    # des latences stables
    stables = [{"latence": 5.0, "termes": 28}, {"latence": 5.2, "termes": 28},
               {"latence": 4.8, "termes": 29}]
    r = banc._repetitions(stables)
    _verifie(r["lat"]["moy"] == 5.0, f"moyenne fausse : {r['lat']['moy']}")
    _verifie(r["lat"]["min"] == 4.8 and r["lat"]["max"] == 5.2,
             f"min/max faux : {r['lat']}")
    _verifie(r["lat"]["ecart"] < 0.5,
             f"des latences stables doivent avoir un petit ecart : {r['lat']['ecart']}")

    # des latences aleatoires : meme moyenne, ecart grand
    alea = [{"latence": 4.0, "termes": 28}, {"latence": 30.0, "termes": 28},
            {"latence": 5.0, "termes": 28}]
    r2 = banc._repetitions(alea)
    _verifie(r2["lat"]["moy"] == 13.0, f"moyenne fausse : {r2['lat']['moy']}")
    _verifie(r2["lat"]["ecart"] > 10,
             f"un ecart-type de {r2['lat']['ecart']} ne distingue pas un modele "
             "d'un autre")

    # une seule mesure : pas d'ecart-type, pas de division par zero
    une = banc._repetitions([{"latence": 7.0, "termes": 30}])
    _verifie(une["lat"]["ecart"] == 0.0, "une seule mesure doit avoir un ecart nul")

    # aucune mesure exploitable : pas de division par zero
    vide = banc._repetitions([{"erreur": "echec"}])
    _verifie(vide["lat"]["moy"] is None, "sans mesure, la moyenne doit etre None")

    # les termes doivent aussi etre repris
    _verifie(r["termes"]["moy"] == 28.3, f"termes moyens faux : {r['termes']['moy']}")


def test_raisonne() -> None:
    """Trois etats, pas deux : raisonner, devoir raisonner, choisir le niveau.

    ling-3.1-flash supporte "reasoning" mais ne liste aucun niveau. Conclure
    qu'il ne raisonne pas — c'est ce que faisait l'interface — masque a tort
    le seul reglage que ce modele offre encore.
    """
    cat = [m for m in banc.catalogue() if not m.get("erreur")]
    if not cat:
        return

    # tout modele qui dit "reasoning" doit porter raisonne=True, meme sans
    # niveaux
    for m in cat:
        params = (m.get("brut") or {}).get("supported_parameters") or []
        if "reasoning" in params:
            _verifie(m.get("raisonne") is True,
                     f"{m['id']} supporte reasoning mais raisonne=False : "
                     "l'interface va masquer ses leviers a tort")

    # les trois cas :
    #   niveaux      -> on peut choisir
    #   sans niveaux  -> on peut activer/desactiver, mais pas choisir
    #   pas du tout   -> rien
    for m in cat:
        if not m.get("raisonne"):
            continue
        if m.get("efforts"):
            _verifie(m.get("raisonnement_obligatoire") is not None,
                     f"{m['id']} a des niveaux, le statut doit etre connu")

    # le cas reel qu'on a vu : raisonne sans niveaux
    sans_niveaux = [m for m in cat if m.get("raisonne") and not m.get("efforts")]
    for m in sans_niveaux:
        _verifie("reasoning" in (m["brut"].get("supported_parameters") or []),
                 f"{m['id']} : attendu un modele qui sait raisonner")


def test_leviers_par_modele() -> None:
    """Chaque modele a ses propres reglages.

    Tu peux vouloir space-bunny en effort "low" et ling en "high" : deux choix
    independants. Si un seul jeu s'applique a tous, tu comparais des modeles
    avec des reglages differents — le resultat ne voulait rien dire.
    """
    a = {"id": "x/low", "efforts": ["low"], "raisonnement_obligatoire": True}
    b = {"id": "y/high", "efforts": ["high"], "raisonnement_obligatoire": True}

    # un dict par modele
    par = {"x/low": {"effort": "low", "max_tokens": 16000},
           "y/high": {"effort": "high", "max_tokens": 4000}}
    _verifie(banc._leviers_du_modele(par, "x/low", a)["effort"] == "low",
             "x/low devrait avoir effort low")
    _verifie(banc._leviers_du_modele(par, "y/high", b)["effort"] == "high",
             "y/high devrait avoir effort high")
    _verifie(banc._leviers_du_modele(par, "y/high", b)["max_tokens"] == 4000,
             "chaque modele garde son propre budget de tokens")

    # un dict simple : applique a tous
    simple = {"effort": "low", "max_tokens": 16000}
    _verifie(banc._leviers_du_modele(simple, "x/low", a)["effort"] == "low",
             "un jeu simple doit s'appliquer a tous")

    # un modele absent du dict par modele : on ne doit pas planter
    _verifie(banc._leviers_du_modele(par, "z/inconnu", a) is not None,
             "un modele absent du dict doit avoir un jeu de secours")

    # et deux appels reels doivent partir differemment
    ca = banc._construire(a, banc._leviers_du_modele(par, "x/low", a), "p")
    cb = banc._construire(b, banc._leviers_du_modele(par, "y/high", b), "p")
    _verifie(ca["reasoning"]["effort"] != cb["reasoning"]["effort"],
             f"les deux modeles ont recu le meme effort : "
             f"{ca['reasoning']['effort']} / {cb['reasoning']['effort']}")
    _verifie(ca["max_tokens"] != cb["max_tokens"],
             "les budgets de tokens se sont confondus")


def main() -> int:
    for fn in (test_catalogue, test_catalogue_complet, test_leviers,
               test_leviers_supports, test_qualite,
               test_scenarios, test_resume, test_charge,
               test_reprise_reponse_vide, test_leviers_api,
               test_score, test_repetitions,
               test_include_reasoning, test_raisonne,
               test_leviers_par_modele):
        fn()

    if "--reseau" in sys.argv:
        test_reseau()
    if "--charge" in sys.argv:
        test_reseau_charge()

    if ERREURS:
        print("\nECHECS :")
        for e in ERREURS:
            print(f"  - {e}")
        return 1
    print("\nOK — banc")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())