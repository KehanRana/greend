# GreenRoute

Local, simulated inference gateway for the Micro-Grid Aware LLM Inference Scheduler project.

**Implemented milestone M1:** authenticated non-streaming text chat, deterministic local mock inference, three simulated endpoint registry entries, preferred healthy endpoint selection, SQLite migration, persisted metadata-only decisions/attempts, activity API and health/readiness.

**Planned:** grid signals and carbon routing (M2), durable async execution (M3), estimates/dashboard (M4), full demo/load verification (M5), conditional live integration (M6). Database tables and job schemas are foundations, not working job endpoints. This version performs no real inference, energy measurement or carbon reduction. All three regions point to one local mock process.

## Quickstart

Run commands from the repository root. Python 3.12+ is required; the checked-in lock was verified on Python 3.14.5/macOS arm64. Other supported Python/platform combinations require their own verification. Node/npm is not required until dashboard implementation.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
cp .env.example .env
```

Replace the gateway key in `.env` with a private local value. Load the environment in **each terminal** used below:

```sh
set -a
source .env
set +a
export PYTHONPATH=backend
```

Apply migrations once before serving:

```sh
python -m alembic upgrade head
```

Start the simulated upstream in terminal 1:

```sh
python -m uvicorn greenroute.mock:app --host 127.0.0.1 --port 8001
```

Start the gateway in terminal 2:

```sh
python -m uvicorn greenroute.app:create_app --factory --host 127.0.0.1 --port 8000
```

Run the official SDK example in terminal 3:

```sh
python examples/interactive.py
```

Inspect persisted metadata:

```sh
curl -H "Authorization: Bearer $GREENROUTE_GATEWAY_KEY" http://127.0.0.1:8000/api/activity
```

Restart the gateway and repeat the activity query: previous metadata remains. Interactive API documentation is at `http://127.0.0.1:8000/docs`; liveness and storage readiness are `/health` and `/ready`.

## Supported contract

`POST /v1/chat/completions` accepts `model="demo-model"`, text-only `messages` with system/user/assistant roles, optional temperature (0–2), optional max_tokens (1–8192), and `stream=false`. Unsupported fields/features return 422; the body limit is 1 MiB. Fixture output and usage are fixed and do not depend on input or token limits; these are interface demonstrations, not generated summaries or measured token usage.

Omitted `x-carbon-priority` or `high` routes immediately by configured rank. Deferred/threshold headers are rejected and refer to the planned `/api/jobs` route. Unknown models have no eligible endpoint and return 503. Endpoint health currently reflects registry flags; automatic probes/failure-based health updates are B-08.

The gateway authenticates with its own key, supplies the distinct mock upstream key, and forwards only an allowlisted metadata header. Upstream failures return sanitized errors (429/502/504); ambiguous timeout is recorded as unknown without replay. A successful completion returns `x-greenroute-request-id`, `x-greenroute-region` and `x-greenroute-mode` headers.

`GET /api/activity?limit=20&offset=0` requires the gateway key. This is a single-user demo: every authorized caller sees the same metadata, without prompt/completion content. Do not expose this prototype remotely. Persistent content cleanup, operator controls and advanced recovery remain later backlog work. If the process dies during forwarding, an attempt may remain running; worker reconciliation is not implemented in M1.

## Verification

```sh
python -m pytest -q
```

Tests create and migrate isolated temporary databases, exercise an official SDK call, recreate the app against the same database, inspect credentials/metadata privacy, verify fallback and errors, and check constraints. See [M1 verification](docs/m1-verification.md) for results and limitations.

The initial schema migration is reversible (`alembic downgrade base`), which deletes its tables. Only use downgrade against disposable demo data. Never commit `.env`, runtime databases or virtual environments.
