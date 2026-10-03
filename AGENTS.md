# AGENTS.md — Traduction PDF

Traduit des lots de PDF anglais vers le français via PDFMathTranslate et un
proxy IA local (Hermes Agent). Aucune clé d'API : tout passe par le proxy.

Ce document s'adresse à un agent qui reprend le projet. Il dit ce qui est
vrai **maintenant**, pas ce qui était prévu.

---

## 1. Où sont les choses

```
Traduction AI V2/              <- la racine, hors du dépôt git
├── PDFMathTranslate/         LE DÉPÔT (code, versionné)
│   ├── base.py              schéma SQLite et accès par projet
│   ├── registre.py          présence, mode, réassociation, purge
│   ├── donnees.py           glossaire, polices, orphelins, export CSV
│   ├── analyser.py           passe d'analyse : contexte, polices, glossaire
│   ├── bench.py              test de performance des modèles
│   ├── traduire.sh           traduction (backend v2)
│   ├── polices.py            charge le mapping de polices pour BabelDOC
│   ├── catalogue_polices.py  catalogue des polices (3 sources)
│   ├── telecharger_polices.py recherche + téléchargement
│   ├── patch_polices.py      injecte les polices dans le FontMapper
│   ├── sitecustomize.py      charge patch_polices au démarrage de Python
│   └── interface/
│       ├── serveur.py        API FastAPI
│       ├── index.html        interface web (une page, sans build)
│       └── PLAN.md           note de conception de l'interface
│
├── Projets/<nom>/            LES DONNÉES, par projet (hors git)
│   ├── source/               les PDF à traduire
│   ├── traduits/             les PDF traduits
│   ├── downloads/            archives de polices téléchargées
│   └── analyse/
│       ├── etat.db          TOUT l'état : contexte, glossaire, polices
│       ├── consignes-contexte.md   ce que l'utilisateur sait (lu par le LLM)
│       ├── consignes-polices.md   idem, pour les polices
│       ├── polices/                les .ttf de remplacement du projet
│       └── *.log                   logs des étapes
│
└── Global/                   DONNÉES GLOBALES, hors projet (hors git)
    ├── bench/                résultats des tests de modèles
    └── polices/              .ttf partagés + polices.csv de mapping
```

**Séparation stricte** : le dépôt ne contient que du programme. Toute donnée
est hors du dépôt, dans `Projets/` (par projet) ou `Global/` (partagée).

---

## 2. Démarrer

Trois choses à lancer, dans cet ordre :

```bash
# 1. le proxy IA — les étapes qui utilisent le LLM en ont besoin
hermes proxy start --provider nous --port 8645

# 2. l'interface web — http://127.0.0.1:8756
cd PDFMathTranslate/interface
../.venv/Scripts/python.exe serveur.py

# 3. (optionnel) une étape en ligne de commande
cd PDFMathTranslate
.venv/Scripts/python.exe analyser.py --projet <nom> --etapes contexte,polices,glossaire
```

Le proxy **n'est jamais bloquant** : s'il est absent, `analyser.py` et
`traduire.sh` avertissent sur stderr et continuent. Une étape sans LLM
(polices avec `--no-download`) doit pouvoir tourner hors ligne. Un échec réel
vaut mieux qu'un refus préventif.

---

## 3. Le workflow, étape par étape

Quatre étapes, à faire **une par une** avec validation manuelle entre chaque.
Ne jamais enchaîner automatiquement : la qualité en dépend.

| Étape | Sans LLM ? | Produit | Clef |
|---|---|---|---|
| **Contexte** | non | table `contexte` + `page_contexte` | LLM + consignes |
| **Polices** | non (LLM pour la recherche) | tables `police` + `police_polices` | aucun pour la détection |
| **Glossaire** | non | tables `glossaire` + `page_glossaire` | LLM + contexte |
| **Traduire** | non | `traduits/*.pdf` | glossaire + polices |

**Règles de parallélisation** (dans `serveur.py`, `CONFLITS`) :

```
contexte ─┬─→ glossaire → traduire
polices  ─┘
```

`contexte` et `polices` sont parallélisables. `glossaire` et `traduire`
bloquent tout. **Le blocage est par projet** : un projet qui traduit n'empêche
pas les autres.

---

## 4. Quatre règles qui ont déjà coûté du temps

### Règle 1 — Jamais de `rm -rf`

Toujours `bash bin/rm-sûr <chemin>` : ça envoie à la corbeille, donc
récupérable. Un `rm -rf` a déjà détruit 3 PDFs source.

Côté interface, `_corbeille()` dans `serveur.py` fait pareil pour les
suppressions depuis l'UI.

### Règle 2 — Ne jamais patcher le code de BabelDOC

Le backend v2 est dans `pdf2zh/kernel/PDFMathTranslate-next.git/`. On n'y
touche pas : les mises à jour upstream casseraient tout.

Pour injecter des polices, on **hérite** de la classe (`patch_polices.py`) et
`sitecustomize.py` remplace la référence dans les 4 modules qui l'instancient.
BabelDOC neutralise `TranslationConfig.font` (`self.font = None  # just ignore
font`), donc il n'y a aucune option à passer.

### Règle 3 — Un commit par feature

Un commit par feature, upgrade de feature, changement majeur ou fix.
**Pas de micro-commits** pendant le développement : on accumule des commits
inutiles qu'il faut relire. On ne commite qu'une fois la fonctionnalité
finie et testée.

Le fork est `fork` (branche par défaut), upstream est `origin`. Pousser vers
`fork`, jamais vers `origin`.

### Règle 4 — Tester réellement, pas « ça devrait marcher »

Les bugs ci-dessous sont invisibles à la lecture et n'apparaissent qu'à
l'exécution :

- **Indentation avalée par un `continue`** : la syntaxe reste valide, le code
  devient mort. `polices.py` le faisait : le mapping n'était jamais chargé.
- **`\n` littéral cassé** par un heredoc : `out.join("` + vrai saut de ligne
  casse le JS. Toujours passer par un fichier `.py`, jamais un heredoc.
- **Comparaison de chaînes** : le CSV stocke `metrofutura.ttf`, le catalogue
  `metrofutura`. Comparer sans extension.
- **`window.confirm` bloque le navigateur** au point de geler le daemon CDP.
  Utiliser une modale HTML.

---

## 5. Le glossaire, en détail

**Tables** : `glossaire` (le terme) et `page_glossaire` (d'où il vient).

`source == target` signifie « ne pas traduire » (nom propre, code). **3 cas
légitimes sur 71** : `Farmerling`, `Imago`, `plastisteel`. Le prompt interdit
ce cas par défaut ; le nombre est un indicateur de qualité du prompt.

**Un terme n'est orphelin que si TOUS ses PDF ont disparu** — c'est une
requête, pas un drapeau :

```sql
WHERE NOT EXISTS (SELECT 1 FROM page_glossaire pg JOIN pdf p ON p.id = pg.pdf_id
                  WHERE pg.glossaire_id = g.id)
```

C'est tout l'intérêt du modèle : un terme trouvé dans 3 PDF survit à la
disparition de 2 d'entre eux.

L'extraction **automatique est désactivée** : elle produit des entrées
`source == cible` qui forcent la non-traduction et dégradent le résultat.

## 6. La base et le registre

**Tout est dans `Projets/<nom>/analyse/etat.db`.** Pas de CSV, pas de JSON :
un seul fichier, des relations.

Le CSV du glossaire n'existe que le temps d'une traduction, dans un fichier
temporaire, parce que `babeldoc/glossary.py` ne lit que
`source, target, target_language`. Un `trap EXIT` le supprime, même en cas
d'échec. `Global/polices.csv` est le seul CSV permanent : il est partagé
entre projets et modifié à la main.

### Ce que tout se rattache à l'empreinte, jamais au nom

`pdf.empreinte` est le hash du **texte** normalisé, pas des octets. Deux
exports du même document diffèrent au niveau octet mais ont le même texte —
c'est le seul moyen de reconnaître un renommage ou un ré-export.

Conséquence vérifiée : renommer `Slipsinger.pdf` en `Slipsinger v2.pdf` à la
main, rattacher les deux fiches, et les **7 polices liées suivent**.

`pdf_noms` garde tous les noms portés par un PDF, donc
`pdf_par_nom(con, ancien)` continue de trouver après un renommage.

### Les deux tables many-to-many

`page_glossaire` et `police_polices` portent les sources. C'est ce qui rend
l'orphelin calculable au lieu d'être un drapeau à maintenir : supprimer un
PDF se répercute partout, sans balayer quoi que ce soit.

### Quatre règles qui ont déjà coûté du temps

1. **`_suivre` n'écrase pas un état posé à la main.** Une pause termine le
   processus, donc son code de retour n'est pas un échec : si `_suivre`
   écrivait `erreur`, le bouton Pause afficherait « erreur ».
2. **Les routes littérales avant les routes paramétrées.**
   `/etape/pause` avant `/etape/{etape}`, sinon la seconde avale la première.
   FastAPI matche dans l'ordre de déclaration.
3. **Une ligne sans remplacement n'est pas un choix.** Elle ne doit pas
   bloquer la cascade vers `Global/polices.csv`.
4. **Le registre est la source de vérité, pas le disque.** `analyser.py`
   synchronise avant d'analyser ; un PDF marqué `ignore` est sauté mais garde
   ses données.

## 7. Les polices, en détail

Trois sources, du plus spécifique au général :

1. `Projets/<nom>/analyse/polices/` — le projet
2. `Global/polices/` — partagé entre tous les projets
3. `~/.cache/babeldoc/fonts/` — 34 polices embarquées, toujours disponibles

**Le mapping se lit en cascade** : une entrée du projet gagne ; sinon celle
du global. Une ligne *sans* remplacement n'est pas une décision, elle ne
bloque donc pas la cascade. Modifier `Global/polices.csv` change le
comportement de tous les projets sans décision propre.

**Le nom du fichier ne décide de rien.** Un `.ttf` nommé `FuturaPT-Book.ttf`
ne rend pas Futura : le nom n'est qu'une clé de correspondance, le rendu
vient du contenu.

---

## 8. Variables d'environnement

| Variable | Effet |
|---|---|
| `PDF2ZH_PROJET` | choisit le projet (sinon le premier trouvé) |
| `PDF2ZH_MODEL` | modèle de traduction |
| `PDF2ZH_TERM_MODEL` | modèle d'extraction de glossaire |
| `PDF2ZH_PROXY` | URL du proxy |
| `PDF2ZH_QPS` / `PDF2ZH_POOL` | débit / parallélisme |
| `PDF2ZH_NO_FONTS` | désactive le patch des polices |

**Modèles** : mesurés par `bench.py`, pas choisis à l'œil. Le classement bouge
d'un test à l'autre, les quotas gratuits étant variables — **relire le dernier
résultat** dans `Global/bench/resultats.json` ou l'onglet Bench.

| Modèle | Réussis | Latence |
|---|---|---|
| `inclusionai/ling-3.0-flash-sante:free` | 6/6 | **1,3 s** |
| `poolside/laguna-s-2.1:free` | 6/6 | 2,8 s |
| `meituan/longcat-2.5-preview:free` | 6/6 | 3,6 s |
| `poolside/laguna-xs-2.1:free` | 6/6 | 4,5 s |

Le modèle d'extraction de glossaire doit supporter
`response_format: json_object` — `ling-sante` renvoie 400.

Le proxy annonce **7 modèles gratuits dont 3 morts** (2× 404, 1× 400). Ne pas
faire confiance à sa liste : vérifier par un test.

---

## 9. Ajouter une étape

1. **Dans `analyser.py`** : une fonction `step_xxx(pdfs, work, con, ...)`,
   qui reçoit la connexion `con` et écrit **en base** — plus aucun fichier.
   Elle émet `prog(fait, total, libelle)` pour la progression.
   Si l'étape a ses propres tables, les ajouter dans `SCHEMA` (`base.py`) et
   écrire les lectures/écritures dans `donnees.py`.
2. **Dans `serveur.py`** : ajouter le nom à `ETAPES`, et ses conflits dans
   `CONFLITS`. Sans ça, aucune règle de blocage ne s'applique.
3. ~~**Dans `ETAPES_TOUT`**~~ — supprimé : l'enchaînement automatique n'existe
   plus, le workflow exige une validation manuelle entre chaque étape.
4. **Dans `index.html`** : `LIB` (libellé), `ORDRE` (ordre du workflow), la
   fonction `vue_xxx`, et le routage dans `dessinerVue()`. Elle lit l'API, pas
   la base directement.
5. **Dans `_etat_projet()`** de `serveur.py` : ajouter le test « fait ? » qui
   sert à la détection de l'étape suivante.

`prog()` émet des lignes `[PROGRESS] fait/total libelle` que le serveur lit
dans le flux du processus. C'est inoffensif en ligne de commande.

---

## 10. L'interface

Une seule page, HTML + JS vanilla, **aucun build**. Servie par `serveur.py`
sur la même origine que l'API.

Dispositions :
- **panneau Projets à gauche**, repliable
- **bannière d'étapes en haut** : chaque bouton devient une barre de
  progression avec son état ; le dernier est « étape suivante »
- **onglets centraux** : Config, Options, Contexte, Polices, Glossaire,
  PDFs, Log ; plus le panneau Bench, global, hors projet

L'état des étapes vit en **mémoire** dans le serveur : un travail en cours ne
survit pas à un redémarrage. L'interface interroge `/etape/suivante` pour
savoir où elle en est.

---

## 11. Dépannage

| Symptôme | Cause probable |
|---|---|
| `pdf2zh_next` ne démarre pas après un déplacement | les 91 lanceurs `.exe` portent des shebangs absolus et des chemins encodés en dur ; il faut les réécrire. Procédure dans le skill `pdfmathtranslate-hermes`, référence `relocating-a-venv.md`. |
| `ModuleNotFoundError: babeldoc` | mauvais interpréteur ; utiliser `.venv/Scripts/python.exe` ou `pdf2zh/kernel/.../.venv/` |
| `proxy injoignable` | le proxy est tombé ; `hermes proxy start --provider nous --port 8645` |
| une étape renvoie 0 | tous les appels ont échoué ; regarder le log de l'étape |
| le port 8756 est occupé | un ancien serveur tourne ; `netstat -ano | grep 8756` puis `taskkill /F /PID` |

**Six tests, à lancer avant de conclure.** Ils tournent en une seconde :

```bash
.venv/Scripts/python.exe test_base.py test_registre.py test_donnees.py test_export.py
cd interface && ../.venv/Scripts/python.exe test_serveur.py test_api.py
```

Ils vérifient le schéma, le registre, la logique d'orphelin, l'export, les
routes et leurs refus. Ils ne remplacent pas l'exécution réelle : c'est
elle qui a attrapé le `with` qui se fermait trop tot, et le `catch` duplique.

Pour le reste, la vérification est d'exécuter et de regarder : `/api/docs` permet de tester chaque route, et
`python polices.py` / `python catalogue_polices.py` affichent l'état réel des
polices.