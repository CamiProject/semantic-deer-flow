# Semantic DeerFlow

[English](./README.md) | [中文](./README_zh.md) | [日本語](./README_ja.md) |
[Français](./README_fr.md) | [Русский](./README_ru.md)

Semantic DeerFlow est un projet dérivé non officiel basé sur
[ByteDance DeerFlow](https://github.com/bytedance/deer-flow). Il n'est ni affilié à
ByteDance ou au projet officiel DeerFlow, ni maintenu ou approuvé par eux.

Ce projet cible les requêtes sémantiques gouvernées et les Actions contrôlées pour les
backends SaaS multi-tenant. Son interface principale est constituée des API Gateway,
Semantic et Action. Le frontend inclus sert actuellement au développement et au
débogage; un frontend produit indépendant n'est pas encore terminé.

## Fonctionnalités principales

- Runtime Agent basé sur LangGraph avec streaming, threads persistants, mémoire,
  checkpoints et compression du contexte.
- Délégation dynamique à des sous-agents avec isolation du contexte et contrôle des
  outils.
- Exécution de fichiers et de commandes dans des sandboxes locales, conteneurisées
  ou distantes.
- Outils intégrés, communautaires, MCP et Skills, avec plusieurs fournisseurs de
  modèles et routage configurable.
- Ontology pour les objets, relations et métriques, complétée par une SQL Scope
  Policy appliquant les restrictions de tables, champs et lignes.
- Actions soumises à proposition, approbation, nouvelle validation IAM, idempotence
  et exécution par un Action Worker isolé.
- APIs Fake IAM/Domain, données SQLite de démonstration et Evals hors ligne pour les
  scénarios de lecture, autorisation, routage et écriture contrôlée.

## Architecture

```text
Navigateur / client API / IM / Scheduler
                  |
               Nginx :2026
                  |
        Frontend + Gateway :8001
                  |
          Semantic API :8003
                  |
             Action Worker
```

Nginx est l'entrée unique. Gateway gère l'authentification, les threads, les runs,
le streaming, la mémoire, les modèles, les Skills, MCP et les tâches planifiées.
Semantic API résout l'Ontology et le Scope, applique la SQL Scope Policy et prépare
les Actions. Seul l'Action Worker doit recevoir les identifiants d'écriture.

## Démarrage

Prérequis : Python 3.12+, Node.js 22+, pnpm, `uv` et GNU Make.

```bash
cp .env.example .env
cp config.example.yaml config.yaml
cp extensions_config.example.json extensions_config.json
make setup
make dev
```

Ouvrez <http://localhost:2026>. Un fournisseur de modèle doit être configuré avant
un run Agent. Le mode démo sémantique utilise SQLite en mémoire et ne requiert pas
MySQL.

| Mode                       | Commande                                |
| -------------------------- | --------------------------------------- |
| Développement local        | `make dev`                              |
| Processus locaux optimisés | `make start`                            |
| Développement Docker       | `make docker-init && make docker-start` |
| Docker production          | `make up`                               |
| Gateway seul               | `cd backend && make dev`                |

Utilisez `make stop`, `make docker-stop` ou `make down` pour arrêter le mode
correspondant.

## Appels API

```bash
curl http://localhost:2026/health
curl http://localhost:2026/api/models

curl -N -X POST http://localhost:2026/api/runs/saas-query/stream \
  -H 'Content-Type: application/json' \
  -H 'X-SaaS-Authorization-Context: <signed-jwt>' \
  -d '{"input":{"messages":[{"role":"user","content":"Count my visible sites."}]}}'
```

L'API compatible LangGraph est disponible sous `/api/langgraph/*`. Les requêtes SaaS
sémantiques utilisent `/api/runs/saas-query/{wait,stream}` ou la variante liée à un
thread `/api/threads/{thread_id}/runs/saas-query/{wait,stream}`. Le JWT signé transporte
tenant, principal, rôles, Scope et version de permission. Voir
[backend/docs/API.md](./backend/docs/API.md).

## Evals

La suite `saas-agent-smoke` contient 12 cas couvrant lecture sémantique, routage,
refus de Scope, approbation, exécution, idempotence et effets secondaires interdits.

```bash
make eval-fixture
make eval-smoke EVALS_GATEWAY_URL=http://127.0.0.1:8001
```

Les Evals nécessitent `DEER_FLOW_ENV=eval`, un modèle configuré et des clés/token
réservés à l'évaluation. L'overlay Docker est
`docker/docker-compose-evals.yaml`; ne l'utilisez pas en production.

## Développement et sécurité

```bash
cd backend && make test && make lint
cd ../frontend && pnpm test && pnpm check
```

Ne versionnez pas `.env`, `config.yaml`, `.deer-flow/`, des identifiants, des données
tenant ou des exports de base de données. Semantic API et Action Worker doivent rester
des services internes. Consultez [SECURITY.md](./SECURITY.md) et
[CONTRIBUTING.md](./CONTRIBUTING.md).

La documentation complète de cette version downstream est disponible en
[anglais](./README.md) et en [chinois](./README_zh.md). La provenance et les
remerciements upstream sont dans [ORIGINAL_README.md](./ORIGINAL_README.md).

Les imports `deerflow.*`, les variables `DEER_FLOW_*`, les API, les structures de
données principales et les identifiants Docker compatibles restent inchangés.
