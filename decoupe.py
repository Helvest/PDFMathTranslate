#!/usr/bin/env python3
"""Decoupage des pages longues, et nettoyage des avertissements d'auteur.

Deux problemes distincts, deux fonctions.

1. L'avertissement de contenu. Un PDF de JDR commence souvent par
   'CONTENT WARNING: ... contains implied sexual violence ...'. Ce n'est pas
   du vocabulaire de jeu, et envoy tel quel pousse le modele aReflectir
   indefiniment : il finit par ne pas repondre du tout. On le retire.

2. La longueur. Au-dela de quelques milliers de caracteres, la reflexion du
   modele s'allonge et le delai de l'amont peut etre depasse. On decoupe alors
   la page en blocs courts — au niveau des paragraphes, jamais au milieu d'un
   mot — et l'appel se fait bloc par bloc.

Aucun LLM, aucun reseau.
"""
from __future__ import annotations

import re

# Avertissements d'auteur : la ligne d'alerte et ce qui la suit sur la meme
# notion. On retire la ligne, pas le reste du document.
BALISES_AVERTISSEMENT = (
    "content warning",
    "trigger warning",
    "maturity warning",
    "avertissement de contenu",
)

# Un bloc ne doit pas depasser ca. space-bunny reflechit proportionnellement
# a la longueur : 1500 caracteres reste sous la zone ou il depasse.
BLOC_MAX = 1500


def nettoyer(texte: str) -> str:
    """Retire l'avertissement d'auteur et ses suites.

    L'avertissement occupe le debut du document et se termine sur une ligne
    vide. C'est cette regle qu'on applique, plutot que d'essayer de deviner
    ou il finit : c'est court, verifiable, et l'avertissement ne peut pas
    reparaitre au milieu d'un paragraphe de jeu.

    On retire les lignes jusqu'au premier blank, la ligne d'avertissement
    comprise.
    """
    lignes = texte.split("\n")

    # l'avertissement doit etre dans les premieres lignes, sinon on ne
    # touche a rien : un mot comme "content" pourrait apparaitre plus tard
    for i, ligne in enumerate(lignes[:5]):
        if any(ligne.strip().lower().startswith(b) for b in BALISES_AVERTISSEMENT):
            debut = i
            break
    else:
        return texte.strip()

    fin = debut
    while fin < len(lignes) and lignes[fin].strip():
        fin += 1

    reste = lignes[fin + 1:]
    return "\n".join(reste).strip()


def decouper(texte: str, taille_max: int = BLOC_MAX) -> list[str]:
    """Decoupe un texte en blocs de taille_max caracteres.

    Au niveau des lignes d'abord ; une ligne plus longue que la limite est
    coupee nette plutot que de casser un mot. L'ordre est conserve et rien
    n'est perdu.
    """
    if len(texte) <= taille_max:
        return [texte]

    blocs: list[str] = []
    courant = ""

    for ligne in texte.split("\n"):
        # une ligne trop longue est coupee nette
        while len(ligne) > taille_max:
            if courant:
                blocs.append(courant.strip())
                courant = ""
            blocs.append(ligne[:taille_max])
            ligne = ligne[taille_max:]

        if len(courant) + len(ligne) + 1 > taille_max and courant:
            blocs.append(courant.strip())
            courant = ligne
        else:
            courant = f"{courant}\n{ligne}" if courant else ligne

    if courant.strip():
        blocs.append(courant.strip())

    return blocs


def _auto_test() -> int:
    print("=== nettoyer ===")
    exemple = (
        "CONTENT WARNING: contains implied sexual violence, forced marriage.\n"
        "Some more warning text here.\n"
        "\n"
        "1. TITHING ALTAR\n"
        "A stone statue guards the door."
    )
    print(decoupe := nettoyer(exemple))
    print(f"\n  {len(exemple)} -> {len(decoupe)} caracteres")

    print("\n=== decouper ===")
    long = "\n\n".join(f"Paragraphe {i} " + "mot " * 100 for i in range(12))
    blocs = decouper(long)
    print(f"  {len(long)} caracteres -> {len(blocs)} bloc(s)")
    for i, b in enumerate(blocs):
        print(f"    {i+1}: {len(b):5} car.  {b[:48]}...")
    return 0


if __name__ == "__main__":
    raise SystemExit(_auto_test())