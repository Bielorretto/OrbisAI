# Orbis AI

Prototype d'attribution des dossiers et des tâches, **IA native**, pour cabinets d'avocats, propulsé par **Mistral**.

1. **Les associés reçoivent des dossiers.** Un dossier peut avoir plusieurs associés. Quand son échéance approche
   (J-x), ils sont notifiés et voient les collaborateurs classés pour ce dossier.
2. **Une équipe par dossier.** Plusieurs avocats travaillent sur un même dossier. Un associé choisit au moins 3
   collaborateurs dans l'ordre : si le n°1 refuse ou ne répond pas à temps, la proposition passe au n°2, puis au n°3.
   Celui qui accepte rejoint l'équipe. On peut ajouter d'autres collaborateurs plus tard, avec la même cascade.
3. **Les collaborateurs organisent le travail.** Chaque collaborateur de l'équipe crée des **tâches**. Il les fait
   lui-même ou les confie à un **stagiaire**, qui rejoint alors l'équipe.
4. **Le classement est fait par l'IA.** Savoir qui a le plus de chances de mener un travail à bien n'est pas une
   question déterministe. Les critères éliminatoires sont des règles fixes, appliquées par le code. Mistral classe
   ensuite les personnes éligibles à partir de **toutes** leurs informations.
5. **Un assistant conversationnel** (Mistral) explique les classements et répond aux questions.

Le temps passé est saisi par tâche, ce qui alimente les budgets, l'avancement des dossiers et la pré-facturation.

## Démarrage

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # puis renseigner MISTRAL_API_KEY
.venv/bin/uvicorn app.main:app --reload
```

Ouvrir http://localhost:8000. Au premier lancement, la base SQLite (`data/loickaton.db`) est créée avec le cabinet
de démonstration décrit dans `app/demo_data.py` : 65 profils et 32 dossiers (voir plus bas). Si le schéma de la base
change, la démo est recréée automatiquement au démarrage.

Tests : `.venv/bin/python -m pytest -q`. Ils tournent sur une base temporaire et en IA simulée, sans réseau.

## Parcours de démo

À la connexion, choisir son rôle puis taper son nom. **exemple** ouvre un vrai profil : Jean Moreau (associé),
Léa Garnier (collaboratrice) ou Hugo Lambert (stagiaire).

1. **Associé « exemple » (Jean Moreau).** Le tableau de bord liste ses dossiers. « Acquisition d'Energie
   Stratégique Française » (priorité 1) est à attribuer. Cliquer sur **Attribuer** : les collaborateurs éligibles
   sont classés par Mistral, chacun avec sa raison. Les personnes exclues sont regroupées, avec leur motif. Le
   bouton **💬 Assistant** répond aux questions.
2. Ajouter les collaborateurs dans l'ordre, puis **Envoyer**.
3. **Changer de profil** et se connecter comme le n°1 pour **accepter** ou **refuser**. Ou bien aller dans
   **Outils de démo**, puis **+4 h** : sans réponse, la proposition passe au suivant.
4. **Collaboratrice « exemple » (Léa Garnier).** Elle a une proposition en attente (AmeriTech) et un dossier en
   cours (NovaPay), avec des tâches. **+ Tâche** crée une tâche, qu'elle fait elle-même ou confie à un stagiaire
   (classement Mistral aussi).
5. **Stagiaire « exemple » (Hugo Lambert).** Il voit une tâche à accepter et une tâche en cours (« J'ai terminé »
   l'envoie en relecture).
6. Sur un dossier en cours, un associé peut **ajouter un collaborateur** à l'équipe.
7. **Mes temps**, puis **Facturation** (pré-facture modifiable, export CSV ou PDF).

## Critères d'attribution et classement par l'IA

Le moteur est dans `app/services/scoring.py`. Il applique le document « Critères d'attribution » en deux étapes.

**1. Critères éliminatoires (règles fixes, vérifiées par le code)**, dans l'ordre : conflit d'intérêts, domaine du
droit non maîtrisé, sous-spécialité non maîtrisée, niveau hiérarchique insuffisant, disponibilité minimale, langue
obligatoire (niveau C1 au moins), juridiction obligatoire. Ces critères sont revérifiés côté serveur quand un associé
envoie sa sélection.

**2. Classement par Mistral** des personnes éligibles. Mistral reçoit :
- le dossier ou la tâche (description complète, domaine, type, échéance, charge, exigences, client pseudonymisé) ;
- la hiérarchie des critères (11 principaux, puis 7 complémentaires) ;
- le profil **complet** de chaque candidat : niveau, ancienneté, domaines et sous-spécialités avec le nombre de
  dossiers traités, langues et niveaux, secteurs, pays, systèmes juridiques, préférences, expérience, charge en
  cours, dossiers passés, heures libres avant l'échéance, date de fin estimée, coût.

Il rend un classement, avec un score sur 100 et une raison courte pour chaque personne. Sa réponse est validée par le
code (identifiants connus, pas de doublon, scores décroissants). Le classement est **mémorisé** : il reste stable
tant que les informations ne changent pas (dossier, équipe, charges), au maximum 24 h. Le bouton **Recalculer**
force un nouveau classement.

Sans Mistral (pas de clé, quota, réseau), un **calcul local** prend le relais : les critères sont pondérés selon leur
rang dans la hiérarchie. L'écran indique quelle méthode a été utilisée.

**Disponibilité minimale.** Pour une tâche, il faut avoir l'effort estimé de libre avant l'échéance. Pour rejoindre
un dossier, il faut 25 % de la charge estimée, avec un plafond de 10 h (`MATTER_MIN_FREE_HOURS`). La charge d'un
collaborateur est sa part du reste à faire des dossiers de son équipe.

**Hiérarchie : associé > collaborateur (collab. senior > collaborateur > junior) > stagiaire.** Les associés se
connectent par « Associé », les seniors, collaborateurs et juniors par « Collaborateur ». Un dossier
complexe exige au moins le niveau collaborateur, ce qui exclut les juniors.

## Données du cabinet (`app/demo_data.py`)

Ces données viennent des documents « Profils Cabinet », « Dossiers du cabinet » et « Critères d'attribution ».
Les informations manquantes ont été complétées :
- **profils** : sous-spécialités, domaines secondaires (pour que chaque dossier ait au moins un candidat), secteurs,
  pays, systèmes juridiques, préférences, expérience contentieuse, transactionnelle et internationale ;
- **dossiers** : client (secteur, langue, pays), échéance, charge estimée, équipe, tâches. La colonne « Rang » du
  tableau devient la **priorité**. Les 3 dossiers du tableau sans description en ont reçu une. Les 3 descriptions en
  double (Horizon Payments, BrightWave, Commission européenne / télécoms) ont été écartées ;
- **renforts** : 33 collaborateurs fictifs (seniors et collaborateurs) s'ajoutent aux 32 profils des documents. Ils
  sont organisés par groupes de compétences : M&A et fiscal, corporate et restructuring, pénal et compliance,
  contentieux et social, propriété intellectuelle et numérique, public et immobilier. Ainsi, **chaque dossier a
  toujours au moins 5 collaborateurs éligibles** (`MIN_ELIGIBLE`, vérifié par les tests) ;
- **répartition** : 16 dossiers en cours (équipes en place, souvent avec plusieurs associés), 1 dossier en cascade
  de proposition et 15 dossiers à attribuer. Les 65 profils travaillent tous sur au moins un dossier ;
- **conflits d'intérêts** : 3 (par exemple Victor Rey avec Meridian Capital).

## Assistant (chatbot)

Le bouton **💬 Assistant** apparaît sur l'écran d'attribution d'un dossier, l'écran de délégation d'une tâche et
leurs pages de détail.
- Un **associé** l'interroge sur le choix des collaborateurs pour un dossier. Un **collaborateur de l'équipe**
  l'interroge sur le choix du stagiaire pour une tâche.
- Il s'appuie sur le même classement que celui affiché (celui de Mistral, mémorisé) et sur les profils complets.
- Exemples : « Qui est le meilleur choix ? », « Pourquoi Sarah plutôt que Camille ? », « Pourquoi Hugo est
  exclu ? », « Comment les critères sont-ils hiérarchisés ? ».
- C'est une vraie conversation avec Mistral. Chaque message est un appel à l'API qui envoie l'historique de la
  discussion et le classement recalculé (critères, motifs d'exclusion, coût, date de fin). Le nom du client n'est
  pas envoyé.
- Garde-fou : si Mistral présente comme exclue une personne qui est éligible, l'application lui demande de corriger
  avant d'afficher la réponse. Si l'erreur persiste, c'est la réponse construite par le code qui est affichée.
- Si Mistral ne répond pas (quota, réseau…), l'erreur est affichée dans le chat, avec 2 nouvelles tentatives
  automatiques en cas de limite de débit. Il n'y a pas de fausse réponse silencieuse. Le mode simulé ne sert que
  sans clé ou avec `AI_MODE=mock`.

## Architecture

100 % Python, sans étape de build : FastAPI, SQLAlchemy et SQLite, avec des pages rendues côté serveur (Jinja) et un
peu de JavaScript natif.

```
app/
  main.py               point d'entrée, planificateur en tâche de fond
  config.py             variables d'environnement + règles métier (délais, poids du score…)
  models.py             schéma de la base (toutes les tables)
  clock.py              horloge de l'app (avançable en démo)
  seed.py               données de démo        → python -m app.seed pour réinitialiser
  labels.py             libellés français des statuts
  services/
    ai.py               SEUL module qui parle à Mistral (+ mode simulé)
    scoring.py          critères éliminatoires, informations transmises à Mistral, classement mémorisé
  demo_data.py          profils, clients et dossiers du cabinet de démonstration
    assistant.py        chatbot : explique le classement (Mistral ou réponse construite par le code)
    workflow.py         cycle de vie : dossiers (cascade, clôture) et tâches (délégation, revue)
    scheduler.py        notifications J-x, expiration des propositions, rappels
    notifications.py    notifications dans l'app, emails, historique
    billing.py          temps, chronomètre, budgets, pré-factures
  routes/               pages et actions HTTP (pages, matters, tasks, billing)
  templates/ static/    interface
tests/                  tests du workflow, de la facturation, de l'IA et des routes
```

Cycle de vie :

```
DOSSIER (attribué par l'associé)
À ATTRIBUER → EN PROPOSITION (collaborateur n°1 → n°2 → n°3)
    ├─ accepté → EN COURS (le collaborateur est responsable, il crée les tâches) → CLÔTURÉ
    └─ 3 refus / expirations → REFUSÉ PAR TOUS → nouvelle sélection par l'associé

TÂCHE (créée par le collaborateur responsable dans son dossier)
À LANCER ─┬─ « je la fais »  → EN COURS ──────────────────────────────────────────→ TERMINÉE
          └─ « je délègue » → DÉLÉGATION EN ATTENTE → EN COURS (stagiaire) → EN REVUE → TERMINÉE
                               (refus du stagiaire : retour à À LANCER)
```

La charge d'un collaborateur, utilisée par les critères de disponibilité, correspond au reste à faire de ses
dossiers en cours (charge estimée moins temps déjà saisi). Celle d'un stagiaire correspond au reste à faire des
tâches qui lui sont déléguées.

## Mistral

Mistral est le seul fournisseur d'IA, et tous les appels passent par `app/services/ai.py` :

| Usage | Variable dans `.env` | Modèle actuel |
|---|---|---|
| Classement des candidats (dossiers et tâches) | `MISTRAL_MODEL_LARGE` | `ministral-14b-latest` |
| Assistant (chatbot) | `MISTRAL_MODEL_CHAT` | `ministral-14b-latest` |
| Analyse d'une tâche (domaine, sous-spécialité, type, complexité, effort) en JSON | `MISTRAL_MODEL_LARGE` | `ministral-14b-latest` |
| Libellés de facturation | `MISTRAL_MODEL_SMALL` | `ministral-8b-latest` |

**Abonnement Mistral.** Avec la clé actuelle, `mistral-large-latest` est refusé (403, non inclus dans l'offre) et
`mistral-small-latest` / `mistral-medium-latest` sont limités à 0 requête par minute (429). Les modèles « ministral »
fonctionnent. Avec une offre supérieure, passer `MISTRAL_MODEL_CHAT` et `MISTRAL_MODEL_LARGE` à
`mistral-large-latest` donnera de meilleures réponses : il suffit de modifier `.env`.

Principes :
- **L'IA assiste, elle ne décide pas.** La cascade, les scores et les montants sont calculés par du code classique,
  et les sorties de Mistral sont validées avant usage.
- `AI_MODE=auto` (par défaut) : si l'API échoue (clé invalide, quota, réseau), l'app bascule en **mode simulé**
  pendant 5 minutes et continue de fonctionner. `mock` force le mode simulé, `live` exige Mistral.
- La clé reste dans `.env` (ignoré par git), côté serveur uniquement. Les noms des clients ne sont pas envoyés à
  Mistral pour le classement.
- L'état de l'IA est visible dans l'en-tête et dans la page **Démo**.

## Règles métier par défaut (`app/config.py`)

| Règle | Valeur |
|---|---|
| Notification de l'associé | J-7 avant l'échéance du dossier (réglable par dossier) |
| Délai de réponse avant passage au suivant | 4 h |
| Nombre minimum de collaborateurs choisis | 3 |
| Le collaborateur sait-il qu'il est le n°2 ? | Non |
| Refus d'un stagiaire | La tâche revient au collaborateur |
| Charge du dossier / effort d'une tâche | Proposés par Mistral, modifiables |
| Domaine d'une tâche | Repris du dossier par défaut |
| Clôture d'un dossier | Par le collaborateur responsable ou l'associé, une fois toutes les tâches terminées |
| Honoraires | Au temps passé uniquement |
| Arrondi du temps | 6 min (1/10e d'heure), au supérieur |
| Temps stagiaire | Non facturable par défaut |
| Alertes de budget | 80 % et 100 % de l'effort estimé |

## Répartition à deux

Les deux domaines correspondent à des fichiers distincts, ce qui limite les conflits git :

- **Dev A, Attribution** : `services/scoring.py`, `services/assistant.py`, `services/workflow.py`,
  `services/ai.py`, `routes/matters.py`, `routes/tasks.py`, `templates/assign.html`, `delegate.html`,
  `matter_new.html`, `matter_detail.html`, `task_new.html`, `task_detail.html`
- **Dev B, Suivi et facturation** : `services/scheduler.py`, `services/notifications.py`, `services/billing.py`,
  `routes/billing.py`, `routes/pages.py`, `templates/timesheet.html`, `billing.html`, `pre_invoice.html`,
  dashboards, agenda (`_person_panel.html`, `routes/common.py`)
- **À deux** : `models.py` (le schéma ne change qu'après discussion), `config.py`, `seed.py`

## Limites connues du prototype

- Pas de vraie authentification : on choisit son rôle (associé, collaborateur, stagiaire) puis on tape son nom,
  sans mot de passe.
- Agendas saisis dans la base, sans synchronisation Outlook/Google.
- Les profils (dossiers traités, secteurs, préférences…) sont saisis dans les données de démo. Il n'y a pas encore
  d'écran pour les modifier.
- Le document mentionne aussi le lien avec la plus-value monétaire et la capacité de facturation. Pour l'instant,
  le coût estimé est affiché mais n'entre pas dans le score.
- Schéma créé par `create_all` : après une modification de `models.py`, réinitialiser la démo. Ajouter Alembic
  pour gérer les migrations au-delà du prototype.
- Le planificateur tourne dans le processus web : un seul worker uvicorn.
- Les emails ne partent que si `SMTP_HOST` est configuré. Sinon, ils sont visibles dans la page Démo.
