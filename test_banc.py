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


def main() -> int:
    for fn in (test_catalogue, test_leviers, test_qualite,
               test_scenarios, test_resume):
        fn()

    if "--reseau" in sys.argv:
        test_reseau()

    if ERREURS:
        print("\nECHECS :")
        for e in ERREURS:
            print(f"  - {e}")
        return 1
    print("\nOK — banc")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())