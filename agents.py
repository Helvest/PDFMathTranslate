#!/usr/bin/env python3
"""Sous-agents d'extraction du glossaire, et agent de fusion.

Le glossaire ne traduit pas. Il sert a deux choses :

  - garder la MEME traduction d'un terme d'un bout a l'autre du document
  - garder INTACT ce qui ne doit pas bouger : noms propres, mots inventes,
    fautes volontaires, bizarreries conservees telles quelles

D'ou la methode. Des sous-agents **aveugles et paralleles** regardent chaque
page isolement et rendent chacun de son cote :
    terme d'origine | traduction suggeree | definition courte
Ils ne se voient pas entre eux, donc ils se recouvrent : c'est voulu. Un agent
de fusion, a la fin, voit tous les doublons d'un coup et tranche avec les
consignes de l'utilisateur et le glossaire deja valide.

Ce que l'utilisateur choisit de leur montrer (tout coche par defaut) est
dans DEFAUTS : consignes, contexte du lot, resume du document, resume de la
page.

Aucun appel reseau ici : ce module fabrique les prompts et lit les reponses.
L'appel se fait dans analyser.py.
"""
from __future__ import annotations

import json
import re

# Parallelisme et taille des blocs. 6 agents : au-dela, le fournisseur limite
# et les coupures amont que nous avons vues reviennent.
AGENTS_PARALLELES = 6
BLOC_CHARS = 1500

# Options de ce que les sous-agents recoivent. Tout coché par defaut.
DEFAUTS = {
    "ctx_consignes": True,
    "ctx_lot": True,
    "ctx_document": True,
    "ctx_page": True,
    "agents_paralleles": AGENTS_PARALLELES,
    "bloc_chars": BLOC_CHARS,
}


# ---------------------------------------------------------------- prompt

PROMPT_EXTRACTION = """Tu constitues un glossaire pour une traduction coherente.

Tu ne traduis PAS le document. Tu reperes des termes dont la traduction doit
rester stable d'un bout a l'autre, et surtout ce qui ne doit PAS bouger.

Pour chaque terme, TROIS CHAMPS — dont une DÉFINITION courte :

A RETENIR pour chaque terme :
- Noms propres : personnes, lieux, organisations, objets nommes, titres.
- Mots ou groupes nominaux techniques du jeu (max 5 mots).
- Tout ce qui est ecrit bizarrement pour que la traduction garde la meme
  bizarrerie : si l'original a une faute volontaire, la traduction doit avoir
  une faute equivalente. Ne corrige rien.
- Les mots en MAJUSCULES de section (BARRACKS, ARMORY...) sont du vocabulaire
  commun : ils se traduisent, sauf si le contexte dit le contraire.

POUR CHACUN, trois champs :
  src        le terme tel qu'il apparait dans le texte, mot pour mot
  tgt        la traduction suggeree, ou le terme IDENTIQUE s'il ne se traduit pas
  definition UNE PHRASE : ce que ce terme designe, d'apres ce que tu vois
             dans ce texte uniquement. Sert a garder la coherence et a
             verifier qu'on parle bien de la meme chose.

REGLE IMPORTANTE : ne corrige aucune faute, aucune orthographe bizarre. Elles
sont voulues.

{corps}

Texte :
---
{texte}
---

Reponds UNIQUEMENT par un tableau JSON, rien autour :
[{{"src": "terme", "tgt": "traduction", "definition": "ce que c'est"}}]
"""


PROMPT_FUSION = """Tu dois faire un glossaire COHERENT — la CONSISTENCE de la traduction est l'objectif, pas la correction, a partir de propositions
independantes. Plusieurs agents ont lu des morceaux differents du meme
document ; ils ont pu proposer des traductions incompatibles.

Tu as aussi un glossaire deja valide par l'utilisateur : ces traductions sont
imposables, tu les conserves.

Regles de decision, dans l'ordre :
1. Si le glossaire existant contient le terme, garde sa traduction mot pour mot.
2. Si les consignes de l'utilisateur l'imposent, applique-les.
3. Sinon, si un intraduitible (nom propre, mot invente, faute voulue, objet
   nomme), src et tgt doivent etre IDENTIQUES. Ne le "corrige" surtout pas.
4. Sinon, si les propositions divergent, choisis la traduction la plus
   coherente avec le ton du jeu, et signale-le dans la definition.
5. Si plusieurs agents ont compris le terme differemment, garde la definition
   la plus informative, et signale le doute.

Propositions :
---json---
{propositions}
---

Glossaire existant (imposable) :
---json---
{existant}
---

Consignes de l'utilisateur :
---
{consignes}
---

Reponds UNIQUEMENT par un tableau JSON :
[{{"src": "terme", "tgt": "traduction", "definition": "definition", "source": "agent|fusion", "doute": "vide ou raison du doute"}}]
"""


def _bloc(consignes: str = "", lot: str = "", document: str = "",
          page: str = "", ctx: dict | None = None) -> str:
    """Assemble les parties optionnelles du prompt d'extraction."""
    ctx = ctx if ctx is not None else DEFAUTS
    morceaux: list[str] = []

    if consignes and ctx.get("ctx_consignes"):
        morceaux.append(f"=== CONSIGNES DE L'UTILISATEUR ===\n{consignes}\n=== FIN ===")
    if lot and ctx.get("ctx_lot"):
        morceaux.append(f"CONTEXTE DU LOT (tous les documents) :\n{lot}")
    if document and ctx.get("ctx_document"):
        morceaux.append(f"RESUME DU DOCUMENT :\n{document}")
    if page and ctx.get("ctx_page"):
        morceaux.append(f"RESUME DE CETTE PAGE :\n{page}")

    return "\n\n".join(morceaux) if morceaux else "(aucun contexte, juge sur le texte seul)"


def prompt_extraction(
    texte: str,
    consignes: str = "",
    lot: str = "",
    document: str = "",
    page: str = "",
    ctx: dict | None = None,
) -> str:
    """Le prompt d'un sous-agent. ctx dicte ce qu'il voit (tout par defaut)."""
    return PROMPT_EXTRACTION.format(
        corps=_bloc(consignes, lot, document, page, ctx),
        texte=texte,
    )


def prompt_fusion(
    propositions: list[dict],
    existant: list[dict] | None = None,
    consignes: str = "",
) -> str:
    """Le prompt de l'agent de fusion."""
    return PROMPT_FUSION.format(
        propositions=json.dumps(propositions, ensure_ascii=False, indent=1),
        existant=json.dumps(existant or [], ensure_ascii=False, indent=1),
        consignes=consignes or "(aucune)",
    )


# ---------------------------------------------------------------- reponse

def lire_reponse(brut: str) -> list[dict]:
    """Extrait le tableau JSON d'une reponse d'agent.

    L'agent bavarde : il peut.ecrire une phrase avant et apres. On cherche
    le premier '[' et le dernier ']'. Une reponse parasite rend une liste
    vide plutot que de lever : un agent qui n'a rien compris ne doit pas
    faire tomber l'etape.
    """
    if not brut:
        return []
    i, j = brut.find("["), brut.rfind("]")
    if i < 0 or j <= i:
        return []
    try:
        donnees = json.loads(brut[i:j + 1])
    except json.JSONDecodeError:
        # le JSON est parfois casse par un guillemet ; on tente une reprise
        donnees = _reprise_json(brut[i:j + 1])
        if not donnees:
            return []

    out = []
    for d in donnees:
        if not isinstance(d, dict):
            continue
        src = str(d.get("src", "")).strip()
        tgt = str(d.get("tgt", "")).strip()
        if not src or not tgt:
            continue  # sans cible, le terme ne sert a rien
        out.append({
            "src": src,
            "tgt": tgt,
            "definition": str(d.get("definition", "") or "").strip(),
        })
    return out


def _reprise_json(texte: str) -> list:
    """Recupere un tableau JSON mal forme — guillemets non echappes, virgules."""
    # on tente de refermer les guillemets orphelins
    corrige = re.sub(r'(?<=[\w\s])"(?=[,}\]])', '\\"', texte)
    # et de retirer les virgules avant une fermeture
    corrige = re.sub(r",(\s*[}\]])", r"\1", corrige)
    try:
        return json.loads(corrige)
    except json.JSONDecodeError:
        return []


# ---------------------------------------------------------------- planning

def planifier(pages: list[tuple[str, int, str]]) -> list[dict]:
    """Une tache par bloc. Une page longue donne plusieurs blocs.

    pages : [(nom_pdf, numero, texte), ...]

    Le decoupage passe par decoupe.py, qui sait aussi couper une ligne sans
    espaces — une page de PDF peut etre un seul bloc de texte colle.
    """
    import decoupe

    taille = int(DEFAUTS.get("bloc_chars") or BLOC_CHARS)

    taches: list[dict] = []
    for nom_pdf, numero, texte in pages:
        texte = (texte or "").strip()
        if not texte:
            continue
        for bloc in decoupe.decouper(texte, taille):
            if bloc.strip():
                taches.append({"pdf": nom_pdf, "page": numero, "texte": bloc.strip()})
    return taches


def _auto_test() -> int:
    print("=== prompt complet (tout coche) ===")
    print(prompt_extraction("Le texte ici.", consignes="CE=ce", lot="LOT", page="PAGE")[:600])
    print("\n=== prompt minimal (tout decoche) ===")
    print(prompt_extraction("Le texte ici.", consignes="CE=ce", lot="LOT", page="PAGE",
                            ctx={"ctx_consignes": False, "ctx_lot": False,
                                 "ctx_document": False, "ctx_page": False})[:260])
    print("\n=== lecture d'une reponse ===")
    for t in lire_reponse('blah [{"src":"A","tgt":"B","definition":"c"}] blah'):
        print("  ", t)
    return 0


if __name__ == "__main__":
    raise SystemExit(_auto_test())