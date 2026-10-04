"""Note de qualité d'un terme de glossaire (0 à 100).

Pourquoi une note calculée plutôt qu'un avis de modèle : une note qu'on ne sait
pas expliquer, on ne peut pas la contester. Ici chaque composante est séparée et
visible, donc une note qui te paraît injustifiée se discute terme par terme.

Une note n'est pas un jugement de la traduction : c'est « est-ce que ce terme
mérite de figurer dans le glossaire ». Un terme absent ne nuit pas
directement, il fait que le même mot sera traduit de deux façons différentes
à deux endroits — ce qu'on cherche précisément à éviter.
"""

# Mots grammaticaux anglais : jamais un terme de glossaire, quel que soit le
# contexte. C'est le seul critère qui élimine sans appel.
GRAMMATICAUX = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "at", "by",
    "for", "with", "from", "as", "is", "are", "was", "were", "be", "been",
    "it", "its", "this", "that", "these", "those", "he", "she", "they", "we",
    "you", "i", "not", "no", "if", "then", "else", "so", "than", "too",
    "can", "will", "would", "shall", "should", "may", "might", "must",
    "have", "has", "had", "do", "does", "did", "there", "here", "when",
    "what", "which", "who", "whose", "how", "all", "any", "some", "each",
    "into", "out", "up", "down", "over", "under", "about", "after", "before",
    "his", "her", "their", "our", "your", "my", "me", "him", "them", "us",
}

# Mots fréquents d'un jeu de rôle : présents partout, ils se traduisent sans
# qu'on ait besoin de le fixer. Vrais, mais inutiles au glossaire.
COMMUN_RPG = {
    "attack", "defense", "defence", "damage", "heal", "spell", "skill",
    "strength", "dexterity", "wisdom", "charisma", "constitution", "speed",
    "level", "health", "mana", "armor", "armour", "weapon", "item", "gold",
    "roll", "dice", "die", "player", "character", "game", "turn", "round",
}


def _plage(valeur: float, mini: float, maxi: float, bas: int, haut: int) -> int:
    """Convertit une valeur en points entre `bas` et `haut`."""
    if maxi <= mini:
        return haut
    ratio = (valeur - mini) / (maxi - mini)
    ratio = max(0.0, min(1.0, ratio))
    return int(round(bas + ratio * (haut - bas)))


def noter(source: str, target: str, definition: str, occurrences: int,
          nb_pdfs: int = 1, valide: bool = False,
          nb_pdfs_projet: int | None = None) -> dict:
    """Note un terme et renvoie le détail du calcul.

    Renvoie `note` (0-100) et `detail` : la liste des composantes avec leurs
    points, pour pouvoir afficher « pourquoi cette note ».
    """
    source = (source or "").strip()
    target = (target or "").strip()
    mot = source.lower().strip(".,;:!?()[]")

    detail: list[dict] = []

    # --- 1. Le terme existe-t-il ? (éliminatoire)
    if not source:
        return {"note": 0, "detail": [{"nom": "vide", "points": 0, "max": 0,
                                        "quoi": "source vide"}],
                "intraduitible": False, "verdict": "invalide"}

    # Un mot grammatical dans un glossaire ne peut pas etre « bon », quel que
    # soit le reste : « the » revient partout, donc l'occurrence lui donne 30/30
    # et il remontait en tete. C'est faux — le fixer ne sert a rien et peut
    #caler une traduction. Plafonne, plutot que de laisser les points mentir.
    if mot in GRAMMATICAUX:
        return {"note": 5,
                "detail": [{"nom": "grammatical", "points": 0, "max": 10,
                            "quoi": f"« {source} » est un mot grammatical : "
                                    "à supprimer du glossaire"}],
                "intraduitible": False, "verdict": "à supprimer"}

    # --- 2. Occurrences : le signal le plus fort. Un terme qui revient partout
    #        est un terme où une divergence ferait vraiment mal.
    detail.append({"nom": "occurrences", "points": _plage(occurrences, 0, 10, 0, 30),
                   "max": 30, "quoi": f"{occurrences} occurrence(s)"})

    # --- 3. Présence dans plusieurs PDF du projet : un terme qui traverse le
    #        livre vaut plus qu'un terme très fréquent dans un seul document.
    #
    #        On ne mesure ça que si le projet a plusieurs PDF. Sinon la note
    #        serait amputée de 15 points pour TOUS les termes, sans distinguer
    #        le meilleur du pire — et un terme comme « Slip » (22 occurrences)
    #        tomberait à 70 pour rien.
    if nb_pdfs_projet is None or nb_pdfs_projet < 2:
        # On ne sait pas si « 1 PDF » veut dire « le seul du projet » ou « 1
        # sur 5 ». Sans le total, la mesure est du bruit : on la laisse neutre.
        detail.append({"nom": "portee", "points": 10, "max": 15,
                       "quoi": "non mesurable (projet d'un seul PDF ou total inconnu)"})
    else:
        detail.append({"nom": "portee", "points": _plage(nb_pdfs, 1, nb_pdfs_projet, 5, 15),
                       "max": 15,
                       "quoi": f"{nb_pdfs} PDF(s) sur {nb_pdfs_projet}"})

    intraduitible = bool(target) and source == target

    # --- 4. Intraduitible : la cible la plus difficile à corriger. Un nom
    #        propre intraduitible est excellant ; un mot commun laissé
    #        identique est un bug. On ne peut pas distinguer les deux sans le
    #        contexte, donc on ne penalise PAS ici : la définition tranche.
    detail.append({"nom": "nature",
                   "points": 10 if intraduitible else 6, "max": 10,
                   "quoi": "intraduitible" if intraduitible else "traduit"})

    # --- 5. Une définition permet de juger. Sans elle, on ne peut pas dire si
    #        le terme est utile — et c'est elle qui distingue un vrai nom
    #        propre d'une formule de crédits.
    if definition and definition.strip():
        pts = _plage(len(definition), 10, 90, 4, 10)
        detail.append({"nom": "definition", "points": pts, "max": 10,
                       "quoi": f"définition de {len(definition)} car."})
    else:
        detail.append({"nom": "definition", "points": 0, "max": 10,
                       "quoi": "aucune définition — injugeable"})

    # --- 6. Longueur : « Slip » est un terme ; « is not affiliated with worlds
    #        by watt » est une phrase qu'un modèle a prise pour un mot.
    mots = source.split()
    if len(mots) > 6:
        detail.append({"nom": "longueur", "points": 0, "max": 15,
                       "quoi": f"{len(mots)} mots — c'est une phrase"})
    elif len(mots) > 3:
        detail.append({"nom": "longueur", "points": 6, "max": 15,
                       "quoi": f"{len(mots)} mots"})
    else:
        detail.append({"nom": "longueur", "points": 15, "max": 15,
                       "quoi": f"{len(mots)} mot(s)"})

    # --- 7. Mots grammaticaux et communs : éliminatoires. Un mot grammatical
    #        dans un glossaire ne sert à rien et pollue les traductions.
    if mot in GRAMMATICAUX:
        detail.append({"nom": "grammatical", "points": 0, "max": 10,
                       "quoi": "mot grammatical — à supprimer"})
    elif mot in COMMUN_RPG:
        detail.append({"nom": "courant", "points": 2, "max": 10,
                       "quoi": "mot courant de jeu de rôle"})
    else:
        detail.append({"nom": "specificite", "points": 10, "max": 10,
                       "quoi": "mot spécifique"})

    # --- 8. Terme validé par le projet : intouchable, et remonté au-dessus
    #        de la moyenne pour qu'il ne disparaisse pas dans le tri.
    if valide:
        detail.append({"nom": "valide", "points": 20, "max": 20,
                       "quoi": "validé par toi"})

    note = sum(d["points"] for d in detail)
    # la validation est un plancher : un terme que tu as validé ne peut pas
    # descendre dans le bas du tableau, quoi qu'en dise le calcul
    if valide:
        note = max(note, 70)
    note = max(0, min(100, note))

    if note >= 70:
        verdict = "bon"
    elif note >= 45:
        verdict = "à vérifier"
    else:
        verdict = "probablement inutile"

    return {"note": note, "detail": detail, "intraduitible": intraduitible,
            "verdict": verdict}


def noter_liste(termes: list[dict]) -> list[dict]:
    """Note un lot de termes déjà au format `{source, target, ...}`."""
    for t in termes:
        r = noter(t.get("source", ""), t.get("target", ""), t.get("definition", ""),
                  t.get("occurrences", 0), t.get("nb_pdfs", 1),
                  bool(t.get("valide")), t.get("nb_pdfs_projet"))
        t["note"] = r["note"]
        t["verdict"] = r["verdict"]
        t["detail"] = r["detail"]
    return termes