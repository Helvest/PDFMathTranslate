# Plan — Interface web multi-projets

## Le besoin

Aujourd'hui tout vit dans un seul `Work/`. Il faut pouvoir mener **plusieurs
traductions en parallèle**, chacune avec ses PDFs, ses contextes, ses polices.

Une interface web locale : créer un projet, le sélectionner, éditer ses
fichiers, lancer les étapes, comparer les PDFs.

## Décisions techniques

| Choix | Pourquoi |
|---|---|
| **FastAPI** | Déjà installé (via Gradio). Rien à ajouter. |
| **HTML + JS vanilla, un seul fichier** | Pas de build, pas de npm, pas de framework. |
| **Pas de base de données** | Un projet = un dossier. `projet.json` pour les métadonnées. |
| **Pas de WebSocket** | Polling 1 s du log. Suffisant et plus simple. |
| **Pas de pdf.js** | Le lecteur PDF natif du navigateur, dans deux iframes. |

**Pourquoi pas Gradio** (pourtant installé) : il impose sa mise en page —
composants empilés verticalement. Impossible de faire une sidebar + 3 colonnes.
FastAPI + HTML donne le contrôle total sans dépendance supplémentaire.

## Structure des projets

```
Traduction AI V2/
├── PDFMathTranslate/           le programme (dépôt git)
├── Projets/
│   ├── Cloud-Empress/
│   │   ├── projet.json         nom, dates, langues
│   │   ├── source/             PDFs à traduire
│   │   ├── traduits/           PDFs traduits
│   │   ├── downloads/          archives de polices téléchargées
│   │   └── analyse/
│   │       ├── consignes-contexte.md
│   │       ├── consignes-polices.md
│   │       ├── contexte.md
│   │       ├── glossaire.csv
│   │       ├── polices.csv
│   │       └── polices/        TTF installés
│   └── Autre-Projet/
└── interface/
    ├── serveur.py              FastAPI
    └── index.html              toute l'interface
```

**Point clé** : cette structure est **exactement** ce que les scripts attendent
déjà. `traduire.sh` accepte `PDF2ZH_WORK=<projet>`, `analyser.py` accepte
`-o <projet>/analyse`. **Aucun script à réécrire.**

## Les panneaux

```
┌──────────────┬────────────────────────────────────────┬─────────────┐
│   PROJETS    │          PANNEAU PRINCIPAL             │  ACTIONS    │
│              │                                        │             │
│  [+ Créer]   │  ┌─ Config ─ Contexte ─ Polices ─────┐  │ ▶ Contexte  │
│              │  │ Glossaire ─ PDFs                  │  │ ▶ Polices   │
│  ▸ Cloud-E   │  └───────────────────────────────────┘  │ ▶ Glossaire │
│  ▸ Autre     │                                        │ ▶ Traduire  │
│              │        (contenu de l'onglet)           │             │
│              │                                        │ ── log ──   │
│              │                                        │ en cours... │
└──────────────┴────────────────────────────────────────┴─────────────┘
```

### Onglet Config
- Éditeur `consignes-contexte.md` (textarea, sauvegarde auto)
- Éditeur `consignes-polices.md`
- Métadonnées : nom, langue source, langue cible

### Onglet Contexte
- `contexte.md` en 3 sections repliables : **lot** / **par document** / **par page**
- Chaque section éditable

### Onglet Polices
- **Détectées** : tableau depuis `polices.csv` (police_origine, famille, PDFs)
- **Tableau de remplacement** : police_origine | remplacement | origine | propose | raison
  - `remplacement` éditable, liste déroulante des TTF disponibles
- **Téléchargements** : archives dans `downloads/` avec leur contenu
  - bouton **installer** → copie le TTF dans `polices/` + remplit `remplacement`

### Onglet Glossaire
- Tableau éditable `source | target | tgt_lng`, avec recherche
- Ajouter / supprimer une ligne
- **Alerte** sur les entrées où `source == target` (le piège connu)

### Onglet PDFs
- Liste des PDFs source avec leur état (analysé ? traduit ?)
- **Comparaison** : clic → deux iframes côte à côte (original | traduit)

### Panneau Actions + Log
- Un bouton par étape, avec état (en cours / terminé / erreur)
- Log en direct (polling 1 s)
- Les étapes longues tournent en arrière-plan

## Le backend

`interface/serveur.py` — FastAPI, ~200 lignes

| Méthode | Route | Rôle |
|---|---|---|
| GET | `/api/projets` | liste |
| POST | `/api/projets` | créer (nom) |
| DELETE | `/api/projets/{nom}` | supprimer (corbeille) |
| GET | `/api/projets/{nom}` | métadonnées + état |
| GET/PUT | `/api/projets/{nom}/fichier/{chemin}` | lire/écrire un fichier éditable |
| POST | `/api/projets/{nom}/pdf` | upload de PDFs |
| GET | `/api/projets/{nom}/pdf/{fichier}?type=source\|traduit` | servir le PDF |
| GET | `/api/projets/{nom}/polices` | détectées + downloads + installées |
| POST | `/api/projets/{nom}/polices/installer` | copier un TTF vers `polices/` |
| POST | `/api/projets/{nom}/etape/{nom}` | lancer une étape |
| GET | `/api/projets/{nom}/log` | log de l'étape en cours |

**Sécurité** : chaque route valide que le chemin reste **dans le dossier du
projet** (rejet de `..`). Seul vrai point de vigilance.

**Lancement des étapes** : `subprocess` vers `analyser.py` / `traduire.sh` avec
`PDF2ZH_WORK` et `-o` positionnés sur le projet. Le log va dans un fichier,
l'UI le lit en polling.

## Ce qu'on ne fait pas

- Pas de base de données — les dossiers suffisent
- Pas de npm / build — HTML + JS vanilla
- Pas de WebSocket — polling 1 s
- Pas d'authentification — outil local
- Pas de pdf.js — lecteur natif du navigateur

## Les étapes d'implémentation

| # | Livrable | Dépend de |
|---|---|---|
| 1 | Migrer `Work/` → `Projets/<nom>/` | — |
| 2 | `serveur.py` : lister / créer / supprimer un projet | 1 |
| 3 | Interface : sidebar + création | 2 |
| 4 | Onglet Config (éditeurs de consignes) | 3 |
| 5 | Onglet Polices (tableau + downloads + installer) | 4 |
| 6 | Onglet Glossaire (tableau éditable) | 4 |
| 7 | Onglet Contexte (lecture / édition) | 4 |
| 8 | Actions : lancer les étapes + log en direct | 5-7 |
| 9 | Onglet PDFs : comparaison côte à côte | 8 |

Les étapes 5, 6 et 7 sont indépendantes : on peut les faire dans n'importe quel
ordre une fois la 4 posée.
