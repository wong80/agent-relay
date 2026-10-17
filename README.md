# Agent Relay

Agent Relay is a small FastAPI service for registering agents, delivering one
task at a time, and recording results. PostgreSQL persists the queue and
attempts, while workers execute tasks on their own machines. The included
worker deterministically returns `input.upper()`.

## Run it with Docker Compose

```bash
docker compose up -d --build
```

This starts two services from `compose.yaml`: `postgres` (PostgreSQL 17, data in
the `postgres-data` volume) and `agent-relay` (the API, published on
<http://127.0.0.1:8080>). The relay waits for PostgreSQL to report healthy
before starting. `docker compose down` stops both and keeps the data; add `-v`
to delete it.

Credentials default to `relay`/`relay` for local use. Override them, or the
published ports, with `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`,
`RELAY_PORT` (default `8080`), and `POSTGRES_PORT` (default `5432`, bound to
loopback only) in the environment or a `.env` file.

Open <http://127.0.0.1:8080/> for the token-based dashboard.
`GET /health` is a liveness check and `GET /ready` verifies database
connectivity and schema (it queries the real tables, so an empty or
unreachable database reports not-ready).

## Run it from source

Start only the database, then run the API with auto-reload:

```bash
docker compose up -d postgres
uv run --frozen uvicorn main:app --reload
```

The API defaults to
`postgresql+psycopg://relay:relay@127.0.0.1:5432/agent_relay`, which is the
compose database. Set `RELAY_DATABASE_URL` to use another PostgreSQL database
(`postgres://` and `postgresql://` URLs are accepted too). This serves on
<http://127.0.0.1:8000>; the examples below use that port, so substitute
`8080` when talking to the compose container.

Register two identities and send a task:

```bash
alice=$(curl -sS -X POST http://127.0.0.1:8000/api/v1/agents \
  -H 'content-type: application/json' -d '{"name":"alice"}')
bob=$(curl -sS -X POST http://127.0.0.1:8000/api/v1/agents \
  -H 'content-type: application/json' -d '{"name":"uppercase"}')
```

The response contains each agent's secret `token` once. Keep it outside source
control. Use `Authorization: Bearer <token>` for all subsequent API calls;
registration is the only unauthenticated endpoint. For a shared installation,
set `RELAY_ENROLLMENT_SECRET` and send it as `X-Enrollment-Secret` when
registering.

## Run the deterministic worker

The worker can register itself and save credentials in a mode-0600 JSON file:

```bash
uv run python main.py worker \
  --base-url http://127.0.0.1:8000 \
  --name uppercase \
  --credentials ./uppercase-credentials.json \
  --worker-id laptop-1
```

For failure/redelivery demonstrations, make local execution intentionally slow
and stop the process after one completion:

```bash
uv run python main.py worker --credentials ./uppercase-credentials.json \
  --slow-seconds 75 --worker-id slow-laptop
```

The worker heartbeats during long work. Killing it leaves the claim leased;
after the 60-second lease expires, another worker can claim the task with a new
token and incremented attempt number. `RELAY_LEASE_SECONDS` and
`RELAY_MAX_ATTEMPTS` are configurable server settings.

An existing credential can also be supplied explicitly (the token is not
written to disk):

```bash
uv run python main.py worker --agent-id agent_123 --token agt_… --worker-id laptop-2
```

## Storage and delivery behavior

`database.py` contains the SQLAlchemy models, the PostgreSQL engine, and the
transaction helper. `storage.py` contains task/claim/recovery operations;
routes and request models are kept in `main.py` and `schemas.py`.

Concurrency is coordinated with PostgreSQL row locks, so it is safe across
any number of API processes:

- **Claim** selects the oldest queued task with `FOR UPDATE SKIP LOCKED`, so
  racing workers each take a different task instead of waiting on one another.
- **Heartbeat, complete, and fail** lock the task row with `FOR UPDATE`, so a
  race between them (or with recovery) has one consistent outcome.
- **Recovery** locks each expired task with `SKIP LOCKED`; a task that is busy
  right now is re-checked on the next pass.
- **Task creation** relies on the `(sender_id, idempotency_key)` unique
  constraint: if two identical requests race, one inserts and the other returns
  that task.

Tables are created on startup (`create_all`); there is no migration tool yet.

Claims are at-least-once and leased for 60 seconds by default. Heartbeats extend
an active lease. A completion or failure must include the recipient's bearer
token and claim token. Repeating the exact terminal request with that claim
token is idempotent; a stale token or different result receives `409`.

## Verify

The test suite covers the main protocol, sender/recipient access boundaries,
hashed claim-token behavior, idempotent terminal retries, concurrent claims,
lease expiry before and after recovery, pagination/error shape, and dashboard
asset serving:

```bash
docker compose up -d postgres
uv run --frozen pytest -q
```

Tests need a running PostgreSQL and never touch the `agent_relay` database.
`conftest.py` points the app at `agent_relay_test` (created automatically;
override with `RELAY_TEST_DATABASE_URL`) and refuses to run against any
database whose name does not contain `test`, because the fixtures drop and
recreate every table. The live integration test starts a real server against
its own throwaway database and drops it afterwards.

Use `--frozen` so uv installs exactly what `uv.lock` pins without rewriting it.

## Kubernetes (kind) and CI

`k8s/` deploys PostgreSQL (StatefulSet with a persistent volume) and the relay
(Deployment, 2 replicas) with readiness checks:

```bash
kind create cluster --name agent-relay
docker build -t agent-relay:local .
kind load docker-image agent-relay:local --name agent-relay
kubectl apply -k k8s/
kubectl -n agent-relay port-forward service/agent-relay 8090:80
```

`.github/workflows/ci.yml` runs the protocol and live integration tests against
a PostgreSQL service, then (only if they pass) builds an image with a unique
tag, loads it into the kind cluster, applies the manifests, and waits for the
rollout. Run it locally with [act](https://nektosact.com/); `.actrc` supplies
the runner image, Docker socket, and `DEPLOY_TO_KIND=true`:

```bash
act push
```

A failing test stops the workflow before the build, so the running version is
left untouched. On GitHub-hosted runners the deploy job is skipped unless the
`DEPLOY_TO_KIND` repository variable is set.

This project intentionally does not include Kubernetes, CI, external brokers,
or an LLM.
