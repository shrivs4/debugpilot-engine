# DebugPilot

A local-first AI debugging assistant. Point it at a public GitHub repository,
describe an issue, approve the commands that reproduce it, and DebugPilot
collects runtime, Git and source evidence, asks a local LLM for a root-cause
hypothesis, validates that hypothesis deterministically, and drafts a (mock)
Jira ticket.

Everything runs on your machine: no paid APIs, no cloud services, no
authentication (local demo only).

```
Browser (localhost:5173)
   │  /api/*  (Vite proxy)
   ▼
web ──► api (FastAPI, :8000) ──► postgres + pgvector (:5433 on the host)
           │
           └──► Ollama on the host  (host.docker.internal:11434)
                  nomic-embed-text · qwen2.5-coder:7b
```

## Repository layout

```
apps/
  api/                 FastAPI backend
    app/
      main.py          app + startup (schema, recovery of interrupted work)
      config.py        all settings, read from the environment
      db.py            engine, sessions, schema bootstrap, health probe
      models/          SQLAlchemy models (mirror db/init/001_schema.sql)
      schemas/         Pydantic request/response models
      routers/         /health, /repositories, /debug-runs
      services/        scanning, chunking, indexing, retrieval, git, commands,
                       stack traces, context, hypothesis, validation, tickets
    db/
      init/001_schema.sql   idempotent schema: vector extension, tables, indexes
      Dockerfile            pgvector/pgvector:pg16 + the init script
    tests/             pytest suite
    Dockerfile
  web/                 React + Vite + TypeScript dashboard
poc/                   the original learning scripts (preserved, see poc/README.md)
scripts/ensure-ollama-models.sh
docker-compose.yml  .env.example  Makefile
```

## 1. Required tools

| Tool | Version used here | Check |
| --- | --- | --- |
| Python | 3.11+ | `python3 --version` |
| Node.js | 22.x | `node --version` |
| npm | 10.x | `npm --version` |
| Docker Desktop (Engine + Compose v2) | 29.x / Compose v5 | `docker --version && docker compose version` |
| Ollama | 0.40+ | `ollama --version` |

Python and Node on the host are only needed to run tests or the dashboard
outside Docker. The Docker images contain their own Python 3.11 and Node 22.

> **macOS note:** the stack uses no host bind mounts, so Docker Desktop does
> not need access to `~/Documents`.

## 2. Python environment (optional, for running backend tests on the host)

```bash
cd apps/api
python3.11 -m venv .venv
source .venv/bin/activate
```

## 3. Backend dependencies (optional on the host; Docker installs them in the image)

```bash
pip install -r apps/api/requirements-dev.txt
```

`tree-sitter-language-pack` downloads parser grammars on first use. The API
image fetches the TypeScript, TSX and JavaScript grammars at build time.

## 4. Frontend dependencies (optional on the host; Docker installs them in the image)

```bash
cd apps/web
npm ci
```

## 5. Ollama models

Pull the models only if they are missing:

```bash
make models          # runs scripts/ensure-ollama-models.sh
```

which is equivalent to checking `ollama list` and then running only the
missing pulls:

```bash
ollama pull nomic-embed-text
ollama pull qwen2.5-coder:7b
```

## 6. Start Ollama

Open the Ollama app, or run `ollama serve` in a terminal. Verify:

```bash
curl http://localhost:11434/api/version
```

Ollama stays on the host; it is not a Compose service. The API container
reaches it at `http://host.docker.internal:11434`.

## 7. Configure

```bash
cp .env.example .env      # or: make env
```

The defaults work as they are. Change `API_HOST_PORT`, `WEB_HOST_PORT` or
`POSTGRES_HOST_PORT` if those host ports are already in use. Postgres defaults
to **5433** so it doesn't clash with a local Postgres on 5432.

## 8. Start the application

```bash
docker compose up --build
```

(`make up` does the same in the background.) Wait until all three services
report healthy:

```bash
docker compose ps
curl http://localhost:8000/health   # "status": "ok", pgvector version, Ollama models
```

`/health` returns `"degraded"` with a clear message when Ollama is
unreachable or a model is missing.

## 9. Open the dashboard

<http://localhost:5173>

## 10. Add and sync the checkout fixture

On **Repositories**, enter

```
https://github.com/shrivs4/debugpilot-checkout-fixture
```

(or click *Use the checkout fixture*), leave the branch empty, and click
**Add repository**. DebugPilot:

- clones it into the API's managed workspace volume (`/workspace/repos/<id>-<owner>__<name>`);
- records the commit SHA;
- indexes it.

The status goes `cloning → indexing → ready`.

**Sync** fetches the branch tip and records the previous and new SHAs, whether
the commit changed, and the changed files. It then rebuilds the index. Nothing
is pushed and the upstream repository is never modified.

## 11 and 12. Run the reproduction with explicit approval

Open **New Debug Run** (or click **Debug** on the repository row):

- **Issue description:** `Checkout fails for legacy coupons because discountType is missing`
- **Setup command:** `npm install`
- **Analysis command:** `npm run repro:legacy`

Click **Review commands**. Each command is shown with *"Run this command inside
the selected repository?"* and must be approved individually. **Run debug
analysis** stays disabled until every command is approved.

Commands are tokenized and executed **without a shell**. Pipes, `&&` and
redirects are rejected. They run as an unprivileged user inside the API
container, with a timeout and a reduced environment.

## 13. Review the diagnosis and the mock Jira draft

The run page polls while the pipeline runs and shows:

- the runtime logs and stack trace;
- retrieved code with semantic, lexical and combined scores;
- `git blame` and the suspect commit;
- the LLM hypothesis;
- every validation check and the overall result.

Click **Create Draft** for the mock Jira ticket. It is stored locally and never
sent anywhere.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | DB, pgvector and Ollama status |
| GET | `/repositories` | List repositories with index counts and latest sync |
| POST | `/repositories` | `{url, branch?}`: validate, then clone and index in the background |
| GET | `/repositories/{id}` | Repository status |
| GET | `/repositories/{id}/syncs` | Clone and sync history |
| POST | `/repositories/{id}/sync` | Fetch latest, detect new commit, re-index |
| POST | `/debug-runs` | `{repository_id, issue_description, setup_command?, analysis_command}`: creates a draft |
| GET | `/debug-runs` | List runs (`?repository_id=`) |
| GET | `/debug-runs/{id}` | Run status and current step |
| POST | `/debug-runs/{id}/approve-command` | `{kind: setup\|analysis, command}`: the text must match exactly |
| POST | `/debug-runs/{id}/run` | Start the pipeline (409 until all commands are approved, 503 if Ollama is down) |
| GET | `/debug-runs/{id}/report` | Full report: evidence, hypothesis, validation, draft |
| POST | `/debug-runs/{id}/jira-draft` | Create or regenerate the mock Jira draft |

Interactive docs: <http://localhost:8000/docs>.

## Tests

```bash
make test-api     # backend, inside the api container (uses a separate <db>_test database)
make test-web     # frontend (Vitest + Testing Library), on the host
```

Backend tests that need Postgres are skipped when it is unreachable. Run them
in the container to cover everything.

## Useful commands

```bash
make logs     # follow api + web logs
make down     # stop (keeps data)
make reset    # stop and delete the database and workspace volumes
```
