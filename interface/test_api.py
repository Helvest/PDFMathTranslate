"""Controle des routes metier du serveur.

    cd interface && ../.venv/Scripts/python.exe test_api.py

test_serveur.py verifie que les routes EXISTENT. Celui-ci verifie qu'elles
repondent juste — une route peut etre declaree et renvoyer n'importe quoi.

Le serveur doit tourner sur :8756 pour le test.
"""
import json
import sys
import urllib.error
import urllib.request
from urllib.parse import quote

BASE = "http://127.0.0.1:8756"
PROJET = "Cloud-Empress"


def api(chemin: str, methode: str = "GET", corps: dict | None = None):
    url = BASE + chemin
    data = json.dumps(corps).encode() if corps is not None else None
    req = urllib.request.Request(
        url, data=data, method=methode,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or "null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or "null")
        except Exception:  # noqa: BLE001
            return e.code, None
    except Exception as e:  # noqa: BLE001
        return 0, str(e)


def _fin(echecs: list[str]) -> int:
    if echecs:
        print("ECHECS :")
        for e in echecs:
            print(f"  - {e}")
        sys.exit(1)
    print("OK — api")
    return 0


def main() -> int:
    echecs: list[str] = []
    p = f"/api/projets/{quote(PROJET)}"

    # --- le registre : la route centrale
    st, reg = api(p + "/pdfs")
    if st != 200:
        return _fin([f"GET /pdfs -> {st}"])
    if "pdfs" not in (reg or {}):
        return _fin(["la reponse ne contient pas 'pdfs'"])
    if not reg["pdfs"]:
        echecs.append("aucun PDF renvoye alors que le projet en a")

    for x in reg["pdfs"]:
        for cle in ("nom", "etat", "mode", "empreinte", "present"):
            if cle not in x:
                echecs.append(f"champ manquant dans un PDF : {cle}")

    # --- le glossaire, avec ses sources
    st, g = api(p + "/glossaire")
    if st != 200:
        echecs.append(f"GET /glossaire -> {st}")
    elif not isinstance(g, list):
        echecs.append(f"/glossaire devrait renvoyer une liste, vu {type(g).__name__}")
    else:
        for t in g[:20]:
            if "sources" not in t:
                echecs.append("un terme n'a pas 'sources'")
                break
            if "orphelin" not in t:
                echecs.append("un terme n'a pas 'orphelin'")
                break

    # --- les orphelines : filtre et compte
    st, o = api(p + "/glossaire?orphelins=1")
    if st != 200:
        echecs.append(f"GET /glossaire?orphelins=1 -> {st}")
    elif not isinstance(o, list):
        echecs.append("/glossaire?orphelins=1 devrait renvoyer une liste")

    st, c = api(p + "/compteurs")
    if st != 200:
        echecs.append(f"GET /compteurs -> {st}")
    else:
        for cle in ("pdfs", "termes", "polices", "termes_orphelins"):
            if cle not in (c or {}):
                echecs.append(f"/compteurs sans '{cle}'")

    # --- les polices
    st, pol = api(p + "/polices")
    if st != 200:
        echecs.append(f"GET /polices -> {st}")
    elif isinstance(pol, list) and pol:
        for x in pol[:20]:
            if "spans" not in x or "sources" not in x:
                echecs.append("une police n'a pas 'spans' ou 'sources'")
                break

    # --- le mode : on ne doit pas pouvoir inventer un mode
    st, _ = api(p + "/pdf/mode", "POST", {"nom": "inexistant.pdf", "mode": "inclus"})
    if st not in (404, 409):
        echecs.append(f"mode d'un PDF inconnu devrait etre refuse, vu {st}")

    st, _ = api(p + "/pdf/mode", "POST", {"nom": "x.pdf", "mode": "bidon"})
    if st != 400:
        echecs.append(f"un mode invalide devrait donner 400, vu {st}")

    return _fin(echecs)


if __name__ == "__main__":
    main()