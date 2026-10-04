# Loickaton

Prototype d'attribution des dossiers et des tâches, **IA native**, pour cabinets d'avocats, propulsé par **Mistral**.

1. **L'associé reçoit des dossiers.** Quand l'échéance d'un dossier approche (J-x), il est notifié et voit les
   collaborateurs classés du plus au moins adapté, selon le document « Critères d'attribution » (voir plus bas).
2. **Il confie le dossier.** Il en choisit au moins 3, dans l'ordre. Si le n°1 refuse ou ne répond pas à temps, le
   dossier part automatiquement au n°2, puis au n°3.
3. **Le collaborateur organise le travail.** Celui qui accepte devient responsable du dossier. Il y crée ses
   **tâches**, puis fait chacune lui-même ou la délègue à un **stagiaire**, choisi dans un classement établi selon
   les mêmes critères.
4. **Un assistant conversationnel** (Mistral) explique les classements : pourquoi une personne est en tête,
   pourquoi une autre est exclue, ce qui départage deux candidats.

Le temps passé est saisi par tâche, ce qui alimente les budgets, l'avancement des dossiers et la pré-facturation.

## Démarrage

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # puis renseigner MISTRAL_API_KEY
.venv/bin/uvicorn app.main:app --reload
```

Ouvrir http://localhost:8000. Au premier lancement, la base SQLite (`data/loickaton.db`) est créée avec un cabinet
fictif : un partner, 2 associés, 9 collaborateurs, 4 stagiaires, 3 semaines d'agenda, et des dossiers et des
tâches dans tous les états. Si le schéma de la base change, la démo est recréée automatiquement au démarrage.

Tests : `.venv/bin/python -m pytest -q`. Ils tournent sur une base temporaire et en IA simulée, sans réseau.

## Parcours de démo

1. Sur l'écran « Qui êtes-vous ? », choisir **Associé** puis taper **exemple**. Le tableau de bord liste les
   **dossiers**, et la cloche affiche ceux qui sont arrivés à J-x. On peut aussi taper le nom d'une personne du
   cabinet fictif, par exemple « Sarah Benali » ou juste « benali ».
2. Ouvrir « Nordis – litige fournisseur » puis **Attribuer**. La liste est classée et chaque collaborateur a son
   explication. Cliquer sur un nom ouvre sa fiche et son agenda, et le bouton **💬 Assistant** répond aux questions.
3. Ajouter 3 collaborateurs, les réordonner si besoin, puis **Envoyer**.
4. **Changer de profil** (en haut à droite), se connecter comme le n°1 et **refuser** : le dossier part au n°2. Ou bien aller dans
   **Démo**, puis **+4 h** : sans réponse, la proposition expire et passe au suivant.
5. Le collaborateur qui accepte devient responsable du dossier. Il clique sur **+ Nouvelle tâche**, décrit la tâche
   (Mistral propose le type et l'effort), puis choisit **Créer et la faire moi-même** ou **Créer et choisir un
   stagiaire** (stagiaires classés, avec l'assistant).
6. Côté **Collaborateur**, le compte **exemple** a déjà un dossier en cours (« Hestia – recouvrement de loyers
   impayés »), sans tâche, et un dossier proposé à accepter ou refuser.
7. Côté **Stagiaire**, le compte **exemple** voit les tâches qu'on lui confie : une à accepter ou refuser, une en
   cours (chrono, puis « J'ai terminé », qui l'envoie en relecture au collaborateur).
8. **Mes temps** : lancer le chronomètre ou saisir une note rapide, et Mistral rédige le libellé de facturation.
   Valider ses brouillons.
9. Revenir en associé : **Facturation**, puis **Préparer la pré-facture**. On peut ajuster les libellés et les
   réductions, valider, puis exporter en CSV ou imprimer en PDF.

La page **Démo** permet d'avancer l'horloge de l'application, de lancer le planificateur, de lire les emails
envoyés (conservés localement tant qu'aucun SMTP n'est configuré) et de réinitialiser les données.

## Critères d'attribution

Le classement (`app/services/scoring.py`) applique le document « Critères d'attribution » dans son ordre
hiérarchique. Les libellés sont dans `app/labels.py`.

1. **Critères éliminatoires.** Un seul suffit à exclure la personne. Dans l'ordre : conflit d'intérêts, domaine du
   droit non maîtrisé, sous-spécialité non maîtrisée, niveau hiérarchique insuffisant, disponibilité minimale
   requise, puis langue et juridiction obligatoires (tableau d'exclusion automatique). Ces critères sont revérifiés
   côté serveur quand l'associé valide sa sélection.
2. **11 critères principaux** (80 % du score) : expertise dans le domaine, expertise dans la sous-spécialité,
   dossiers similaires traités, expérience du type de dossier, charge de travail, dossiers en cours, échéances à
   venir, secteur du client, connaissance du client, historique de relation, langue maternelle du client.
3. **7 critères complémentaires** (20 %) : expérience internationale, pays, système juridique, expérience
   contentieuse, expérience transactionnelle, ancienneté au cabinet, préférences personnelles.

Dans chaque groupe, le poids suit le rang : sur n critères, le 1er pèse n et le dernier 1. Un critère sans objet
pour la tâche (pas de sous-spécialité imposée, par exemple) est ignoré. Les égalités sont départagées critère par
critère, dans le même ordre. Ces réglages sont dans `app/config.py` (`MAIN_CRITERIA_SHARE`,
`MIN_AVAILABILITY_RATIO`).

Pour aider à la décision, chaque candidat affiche aussi son **coût estimé** (effort × taux horaire) et une **date de
fin estimée**, calculée à partir des créneaux libres de son agenda. Ce ne sont pas des critères de score.

La charge de travail, plus élevée qu'un autre candidat, n'est pas éliminatoire mais fortement pénalisée, comme le
prévoit le document. Seule la disponibilité minimale est éliminatoire.

Hiérarchie du cabinet : **partner > associé > collaborateur > stagiaire**. Les partners et les associés attribuent
les tâches (ils se connectent par le cercle « Associé ») et les collaborateurs les reçoivent. Une tâche peut exiger
le niveau « collaborateur », ce qui interdit de la déléguer à un stagiaire.

## Assistant (chatbot)

Le bouton **💬 Assistant** apparaît sur l'écran d'attribution d'un dossier, l'écran de délégation d'une tâche et
leurs pages de détail.
- L'**associé** l'interroge sur le choix du collaborateur pour un dossier. Le **collaborateur responsable**
  l'interroge sur le choix du stagiaire pour une tâche.
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
    scoring.py          critères d'attribution (dossier -> collaborateurs, tâche -> stagiaires), charge, date de fin
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
| Assistant (chatbot) | `MISTRAL_MODEL_CHAT` | `ministral-14b-latest` |
| Analyse d'une tâche (domaine, sous-spécialité, type, complexité, effort) en JSON | `MISTRAL_MODEL_LARGE` | `ministral-14b-latest` |
| Phrase d'explication par candidat, libellés de facturation | `MISTRAL_MODEL_SMALL` | `ministral-8b-latest` |
| Proximité sémantique tâche ↔ profils (dans le critère « dossiers similaires ») | `MISTRAL_EMBED_MODEL` | `mistral-embed` |

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
  sans mot de passe. Les partners passent par « Associé ».
- Agendas saisis dans la base, sans synchronisation Outlook/Google.
- Les profils (dossiers traités, secteurs, préférences…) sont saisis dans les données de démo. Il n'y a pas encore
  d'écran pour les modifier.
- Le document mentionne aussi le lien avec la plus-value monétaire et la capacité de facturation. Pour l'instant,
  le coût estimé est affiché mais n'entre pas dans le score.
- Schéma créé par `create_all` : après une modification de `models.py`, réinitialiser la démo. Ajouter Alembic
  pour gérer les migrations au-delà du prototype.
- Le planificateur tourne dans le processus web : un seul worker uvicorn.
- Les emails ne partent que si `SMTP_HOST` est configuré. Sinon, ils sont visibles dans la page Démo.
