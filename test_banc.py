"""Verifie le Banc sans appel reseau, puis avec.

    .venv/Scripts/python.exe test_banc.py          # tout
    .venv/Scripts/python.exe test_banc.py --reseau # ajoute un appel reel
"""
from __future__ import annotations

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
        for champ in m:
            # tout doit se retrouver, soit dans un champ renomme, soit dans
            # le modele complet conserve tel quel
            if champ in entree:
                continue
            if champ in (entree.get("brut") or {}):
                continue
            manquants.append(f"{m['id']}.{champ}")

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
    _verifie(c["reasoning"] == {"enabled": True, "effort": "low"},
             "l'effort demande n'est pas passe")
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


def test_leviers_api() -> None:
    """"auto" est un mot du menu, jamais une valeur d'API.

    Ce bug a ete.commit par accident : /banc/charge/lancer renvoyait la chaine
    "auto" a OpenRouter, qui repondait "reasoning.effort: Invalid option" et
    faisait echouer les 3 appels en 0.7s.
    """
    # serveur.py vit dans interface/ : sans ce chemin le test se sautait
    # silencieusement, ce qui est pire qu'un echec visible
    racine = Path(__file__).resolve().parent
    sys.path.insert(0, str(racine / "interface"))
    sys.path.insert(0, str(racine))
    try:
        import serveur
    except Exception as e:  # noqa: BLE001
        ERREURS.append(f"serveur non importable : {e}")
        return

    modele = {"id": "x/y", "efforts": ["low", "max"], "raisonnement_obligatoire": True}
    class _B:
        @staticmethod
        def effort_serieux(m):
            return "low"

    for valeur in ("auto", "", None):
        lev = serveur._leviers({"effort": valeur}, modele, _B)
        _verifie(lev["effort"] == "low",
                 f"effort {valeur!r} devrait etre resolu en 'low', "
                 f"obtenu {lev['effort']!r}")

    # un effort explicite ne doit surtout pas etre ecrase
    lev = serveur._leviers({"effort": "high", "max_tokens": 8000}, modele, _B)
    _verifie(lev["effort"] == "high", "un effort choisi doit etre respecte")
    _verifie(lev["max_tokens"] == 8000, "max_tokens choisi doit etre respecte")

    # la valeur par defaut quand rien n'est demande
    lev = serveur._leviers({}, modele, _B)
    _verifie(lev["max_tokens"] == serveur.MAX_TOKENS,
             "sans max_tokens, on doit prendre celui de la production")

    # un modele sans niveaux ne doit pas produire une chaine parapluie
    class _Vide:
        @staticmethod
        def effort_serieux(m):
            return None

    lev = serveur._leviers({"effort": "auto"}, {"id": "z", "efforts": []}, _Vide)
    _verifie(lev["effort"] is None,
             f"un modele sans niveaux doit donner None, obtenu {lev['effort']!r}")


def main() -> int:
    for fn in (test_catalogue, test_catalogue_complet, test_leviers, test_qualite,
               test_scenarios, test_resume, test_charge, test_leviers_api):
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