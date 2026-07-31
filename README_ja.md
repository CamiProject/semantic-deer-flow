# Semantic DeerFlow

[English](./README.md) | [中文](./README_zh.md) | [日本語](./README_ja.md) |
[Français](./README_fr.md) | [Русский](./README_ru.md)

Semantic DeerFlow は
[ByteDance DeerFlow](https://github.com/bytedance/deer-flow) を基にした非公式の
ダウンストリームプロジェクトです。ByteDance または DeerFlow 公式プロジェクトとの
提携、保守、推奨関係はありません。

本プロジェクトは、マルチテナント SaaS バックエンド向けの統制されたセマンティック
クエリと承認付き Action に重点を置いています。主な統合面は Gateway、Semantic、
Action API です。同梱のフロントエンドは現在、開発とデバッグ用であり、独立した製品版
フロントエンドはまだ完成していません。

## 主な機能

- ストリーミング、永続 Thread、Checkpoint、長期メモリ、コンテキスト圧縮を備えた
  LangGraph ベースの Agent Runtime。
- コンテキストとツール権限を分離した動的な SubAgent 委譲。
- ローカル、コンテナ、Provisioner、クラウド Sandbox によるファイル・コマンド実行。
- 組み込み、Community、MCP、Skill ツールと、設定可能なモデル Provider・モデルルーティング。
- オブジェクト、関係、メトリクスを定義する Ontology と、テーブル・フィールド・行を
  制約する SQL Scope Policy。
- 提案、承認、IAM 再検証、冪等性、監査を経て、分離された Action Worker が実行する
  制御付き Action。
- Fake IAM/Domain API、SQLite デモデータ、読み取り・権限・ルーティング・Action を
  検証するオフライン Evals。

## アーキテクチャ

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

Nginx が統一入口です。Gateway は認証、Thread、Run、Streaming、Memory、Model、
Skills、MCP、Scheduled Task を管理します。Semantic API は Ontology と Scope を解決し、
SQL Scope Policy と Action preflight を適用します。書き込み資格情報を受け取るのは
Action Worker だけです。

## クイックスタート

必要環境は Python 3.12+、Node.js 22+、pnpm、`uv`、GNU Make です。

```bash
cp .env.example .env
cp config.example.yaml config.yaml
cp extensions_config.example.json extensions_config.json
make setup
make dev
```

<http://localhost:2026> を開きます。Agent Run には Model Provider の設定が必要です。
Semantic demo はインメモリ SQLite を使用するため MySQL は不要です。

| モード             | コマンド                                |
| ------------------ | --------------------------------------- |
| ローカル開発       | `make dev`                              |
| 最適化ローカル実行 | `make start`                            |
| Docker 開発        | `make docker-init && make docker-start` |
| Docker production  | `make up`                               |
| Gateway のみ       | `cd backend && make dev`                |

停止には、それぞれ `make stop`、`make docker-stop`、`make down` を使用します。

## API の使用

```bash
curl http://localhost:2026/health
curl http://localhost:2026/api/models

curl -N -X POST http://localhost:2026/api/runs/saas-query/stream \
  -H 'Content-Type: application/json' \
  -H 'X-SaaS-Authorization-Context: <signed-jwt>' \
  -d '{"input":{"messages":[{"role":"user","content":"Count my visible sites."}]}}'
```

LangGraph 互換 API は `/api/langgraph/*` です。SaaS Semantic Query は
`/api/runs/saas-query/{wait,stream}`、または Thread 付きの
`/api/threads/{thread_id}/runs/saas-query/{wait,stream}` を使用します。署名 JWT は
tenant、principal、roles、Scope、permission version を渡します。詳細は
[backend/docs/API.md](./backend/docs/API.md) を参照してください。

## Evals

`saas-agent-smoke` の 12 case は Semantic Read、Routing、Scope 拒否、Approval、
Execution、Idempotency、禁止された副作用を検証します。

```bash
make eval-fixture
make eval-smoke EVALS_GATEWAY_URL=http://127.0.0.1:8001
```

Evals には `DEER_FLOW_ENV=eval`、設定済みモデル、eval 専用 Key/Token が必要です。
Docker overlay は `docker/docker-compose-evals.yaml` です。本番環境では使用しないでください。

## 開発とセキュリティ

```bash
cd backend && make test && make lint
cd ../frontend && pnpm test && pnpm check
```

`.env`、`config.yaml`、`.deer-flow/`、資格情報、tenant data、DB export を commit
しないでください。Semantic API と Action Worker は内部サービスとして運用します。
[SECURITY.md](./SECURITY.md) と [CONTRIBUTING.md](./CONTRIBUTING.md) も参照してください。

完全なダウンストリーム文書は [英語](./README.md) と
[中国語](./README_zh.md) を参照してください。上流の出典と謝辞は
[ORIGINAL_README.md](./ORIGINAL_README.md) にあります。

`deerflow.*` import、`DEER_FLOW_*` 環境変数、既存 API、主要データ構造、Docker の
互換識別子は維持されています。
