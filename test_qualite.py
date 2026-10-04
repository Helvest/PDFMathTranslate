"""Tests de la note de qualité des termes de glossaire.

Chaque test correspond à un défaut réellement observé sur le glossaire de
`Test-Pipeline`, pas à une idée théorique. Les valeurs attendues sont celles
qui rendent le tri utile en pratique.
"""

import qualite


def _notes(source, occurrences, definition="une definition correcte",
           target=None, **kw):
    return qualite.noter(source, target if target is not None else source,
                         definition, occurrences, **kw)


def main() -> int:
    echecs: list[str] = []

    def verifie(cond, message):
        if not cond:
            echecs.append(message)

    # --- le classement doit être juste sur le glossaire réel
    # 77 termes a une seule note(e) une fois, x22 occurrences
    n = _notes("Slip", 22)["note"]
    verifie(n >= 80, f"le mot-titre du livre doit etre excellent, il a {n}")

    # une formule de credits, une fois seulement, doit tomber tres bas
    n = _notes("Written by Emiel Boven", 1, target="Écrit par Emiel Boven")["note"]
    verifie(n < 50, f"une formule de credits doit etre basse, elle a {n}")

    # --- un mot grammatical est eliminatoire
    # « the » revient partout : l'occurrence lui donne 30/30 et il remontait
    # en tete du classement. Impossible : le fixer ne sert a rien.
    for mot in ("the", "of", "and", "with", "to", "it", "that"):
        r = qualite.noter(mot, mot, "un mot anglais", 22, nb_pdfs=5)
        verifie(r["note"] < 20,
                f"« {mot} » est grammatical : il a {r['note']}/100, il doit etre elimine")
        verifie(r["verdict"] == "à supprimer",
                f"« {mot} » : verdict {r['verdict']}, attendu « à supprimer »")

    # un mot grammatical ne doit pas etre sauvé par une grosse definition
    r = qualite.noter("the", "the", "d" * 200, 99, nb_pdfs=5, valide=True)
    verifie(r["note"] < 30,
            f"« the » valide ne doit pas remonter : il a {r['note']}")

    # --- la portee ne doit jamais faire baisser la note
    # un projet de 1 PDF notait MIEUX qu'un projet de 5 ou le terme est partout
    sans = [_notes("Slip", 22, nb_pdfs=p)["note"] for p in (1, 2, 3, 4, 5)]
    verifie(len(set(sans)) == 1,
            f"sans le total du projet, la portee doit etre neutre : {sans}")

    avec = [_notes("Slip", 22, nb_pdfs=p, nb_pdfs_projet=5)["note"]
            for p in (1, 2, 3, 4, 5)]
    verifie(avec == sorted(avec),
            f"la portee doit monter avec le nombre de PDF : {avec}")
    verifie(avec[0] < avec[-1],
            f"un terme partout doit noter plus haut qu'un terme localise : {avec}")

    # --- une source vide
    r = qualite.noter("", "x", "", 0)
    verifie(r["note"] == 0, f"une source vide vaut 0, elle a {r['note']}")
    verifie(r["verdict"] == "invalide", f"verdict {r['verdict']}")

    # --- une phrase n'est pas un terme
    phrase = "is not affiliated with worlds by watt"
    n = qualite.noter(phrase, phrase, "une phrase", 1)["note"]
    verifie(n < 45, f"une phrase de 8 mots doit etre basse, elle a {n}")

    # --- la validation est un plancher
    # un terme que tu as valide ne peut pas descendre dans le bas du tableau,
    # quoi qu'en dise le calcul
    note_valide = _notes("Written by", 1, definition="", target="Écrit par",
                         valide=True)["note"]
    verifie(note_valide >= 70,
            f"un terme valide ne doit pas descendre sous 70, il a {note_valide}")

    # --- le detail est toujours present et les points sont coherents
    r = qualite.noter("Slip", "Slip", "Force magique", 22, nb_pdfs=1)
    verifie(bool(r["detail"]), "le detail du calcul doit etre fourni")
    somme = sum(d["points"] for d in r["detail"])
    verifie(somme == r["note"],
            f"le detail doit-slider a la note : {somme} vs {r['note']}")
    verifie(0 <= r["note"] <= 100, f"la note doit rester dans 0-100 : {r['note']}")

    # chaque composante a un nom unique : deux « nature » dans le meme detail
    # rendaient l'affichage ambigu
    noms = [d["nom"] for d in r["detail"]]
    verifie(len(noms) == len(set(noms)), f"noms de composante dupliques : {noms}")

    # --- noter_liste ne perd rien
    lot = [{"source": "Slip", "target": "Slip", "definition": "force",
            "occurrences": 5, "nb_pdfs": 1}]
    res = qualite.noter_liste([dict(t) for t in lot])
    verifie("note" in res[0] and "verdict" in res[0] and "detail" in res[0],
            "noter_liste doit renvoyer note, verdict et detail")
    verifie(res[0]["source"] == "Slip", "noter_liste doit preserver les termes")

    if echecs:
        print("ECHECS :")
        for e in echecs:
            print(f"  - {e}")
        print(f"\n{len(echecs)} echec(s)")
        return 1
    print("OK — qualite")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())