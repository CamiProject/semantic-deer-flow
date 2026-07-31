# Semantic DeerFlow

[English](./README.md) | [中文](./README_zh.md) | [日本語](./README_ja.md) |
[Français](./README_fr.md) | [Русский](./README_ru.md)

Semantic DeerFlow является неофициальным downstream-проектом на основе
[ByteDance DeerFlow](https://github.com/bytedance/deer-flow). Проект не связан с
ByteDance или официальным DeerFlow, не поддерживается и не одобряется ими.

Проект предназначен для управляемых семантических запросов и контролируемых Actions
в многопользовательских SaaS backend-системах. Основной интерфейс интеграции — API
Gateway, Semantic и Action. Встроенный frontend сейчас предназначен только для
разработки и отладки; отдельный продуктовый frontend еще не готов.

## Основные возможности

- Agent Runtime на базе LangGraph с потоковым выполнением, постоянными threads,
  checkpoints, долговременной памятью и сжатием контекста.
- Динамическое делегирование SubAgent с изоляцией контекста и контролем доступа к
  инструментам.
- Выполнение файловых операций и команд в локальных, контейнерных и облачных
  sandboxes.
- Встроенные, community-, MCP- и Skill-инструменты, несколько model providers и
  настраиваемая маршрутизация моделей.
- Ontology для объектов, связей и метрик вместе с SQL Scope Policy, ограничивающей
  базы, таблицы, поля и строки.
- Контролируемые Actions с предложением, одобрением, повторной IAM-проверкой,
  идемпотентностью, аудитом и изолированным Action Worker.
- Fake IAM/Domain API, демонстрационные данные SQLite и offline Evals для проверки
  чтения, авторизации, маршрутизации и управляемой записи.

## Архитектура

```text
Browser / API client / IM / Scheduler
                 |
              Nginx :2026
                 |
        Frontend + Gateway :8001
                 |
          Semantic API :8003
                 |
             Action Worker
```

Nginx служит единым входом. Gateway отвечает за аутентификацию, threads, runs,
streaming, memory, модели, Skills, MCP и scheduled tasks. Semantic API разрешает
Ontology и Scope, применяет SQL Scope Policy и выполняет Action preflight. Только
Action Worker должен получать учетные данные для записи.

## Быстрый старт

Требуются Python 3.12+, Node.js 22+, pnpm, `uv` и GNU Make.

```bash
cp .env.example .env
cp config.example.yaml config.yaml
cp extensions_config.example.json extensions_config.json
make setup
make dev
```

Откройте <http://localhost:2026>. Для Agent Run необходимо настроить model provider.
Semantic demo использует SQLite в памяти и не требует MySQL.

| Режим                             | Команда                                 |
| --------------------------------- | --------------------------------------- |
| Локальная разработка              | `make dev`                              |
| Оптимизированный локальный запуск | `make start`                            |
| Docker development                | `make docker-init && make docker-start` |
| Docker production                 | `make up`                               |
| Только Gateway                    | `cd backend && make dev`                |

Для остановки используйте `make stop`, `make docker-stop` или `make down`.

## Вызовы API

```bash
curl http://localhost:2026/health
curl http://localhost:2026/api/models

curl -N -X POST http://localhost:2026/api/runs/saas-query/stream \
  -H 'Content-Type: application/json' \
  -H 'X-SaaS-Authorization-Context: <signed-jwt>' \
  -d '{"input":{"messages":[{"role":"user","content":"Count my visible sites."}]}}'
```

LangGraph-совместимый API доступен по `/api/langgraph/*`. Семантические SaaS-запросы
используют `/api/runs/saas-query/{wait,stream}` или thread-вариант
`/api/threads/{thread_id}/runs/saas-query/{wait,stream}`. Подписанный JWT передает
tenant, principal, roles, Scope и permission version. Полное описание находится в
[backend/docs/API.md](./backend/docs/API.md).

## Evals

Набор `saas-agent-smoke` содержит 12 cases для semantic read, routing, отказа Scope,
approval, execution, idempotency и запрещенных побочных эффектов.

```bash
make eval-fixture
make eval-smoke EVALS_GATEWAY_URL=http://127.0.0.1:8001
```

Evals требуют `DEER_FLOW_ENV=eval`, настроенную модель и отдельные eval key/token.
Docker overlay находится в `docker/docker-compose-evals.yaml` и не должен применяться
в production.

## Разработка и безопасность

```bash
cd backend && make test && make lint
cd ../frontend && pnpm test && pnpm check
```

Не добавляйте в git `.env`, `config.yaml`, `.deer-flow/`, учетные данные, tenant data
или DB export. Semantic API и Action Worker должны оставаться внутренними сервисами.
См. [SECURITY.md](./SECURITY.md) и [CONTRIBUTING.md](./CONTRIBUTING.md).

Полная документация downstream-версии доступна на [английском](./README.md) и
[китайском](./README_zh.md). Происхождение и благодарности upstream описаны в
[ORIGINAL_README.md](./ORIGINAL_README.md).

Импорты `deerflow.*`, переменные `DEER_FLOW_*`, существующие API, основные структуры
данных и совместимые Docker-идентификаторы сохранены.
