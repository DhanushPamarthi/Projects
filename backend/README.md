# TestIQ Backend

FastAPI service for BRD ingestion, source-package creation, and Requirements Analysis.

## What this service does

- Accepts BRD files and supporting sources (`.docx`, `.pdf`, `.txt`, `.md`, `.json`, `.yaml`, `.yml`, `.sql`, `.csv`). JSON is syntax-validated; other structured sources are preserved and extracted as UTF-8 text.
- Preserves original files and immutable source revisions.
- Extracts traceable fragments with paragraph, table, page, or line references.
- Uses OpenAI extraction when configured and local extraction as a fallback.
- Creates immutable source-package manifests.
- Generates versioned Requirements Analysis contracts from the selected package.
- Supports reviewer decisions, comments, accepted assumptions, ambiguity/conflict findings, and readiness status.

Knowledge Fabric/GraphRAG is intentionally not part of this POC. Requirements Analysis reads the frozen source-package fragments directly.

## Prerequisites

- Python 3.11+
- PostgreSQL 14+ for the shared setup, or SQLite for local development
- `uv` or `pip`
- Optional: OpenAI API key for model-assisted extraction

## Install

From `backend/testiq-poc/backend`:

```powershell
uv venv
.venv\Scripts\Activate.ps1
uv pip install -e .
```

Or:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Configure

Copy `.env.example` to `.env` and adjust the values:

```env
DATABASE_URL=postgresql://testiq:testiq@127.0.0.1:5432/testiq
TESTIQ_DATA_DIR=./data
TESTIQ_DB_PATH=./data/testiq.db
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4o-mini
OPENAI_TIMEOUT_SECONDS=90
MAX_UPLOAD_BYTES=26214400
# REQUIRED_BRD_SECTIONS=Scope,Requirements,Acceptance Criteria
```

If `DATABASE_URL` is empty, the service uses local SQLite at `TESTIQ_DB_PATH`. The application creates the schema and tables at startup. Original uploads are stored under `TESTIQ_DATA_DIR/uploads`.

For PostgreSQL, create the role and database once:

```sql
CREATE ROLE testiq LOGIN PASSWORD 'testiq';
CREATE DATABASE testiq OWNER testiq;
```

## Run

From this directory:

```powershell
uv run python -m uvicorn app.main:app --reload --env-file .env --host 127.0.0.1 --port 8001
```

Endpoints:

- Health: <http://localhost:8001/health>
- Swagger: <http://localhost:8001/docs>
- OpenAPI: <http://localhost:8001/openapi.json>

Expected health response:

```json
{"status":"ok","service":"source-package"}
```

## User workflow

1. Create a project, or let the frontend create one during the first BRD upload.
2. Upload a BRD with `POST /api/v1/projects/{project_id}/sources`.
3. Poll `GET /api/v1/revisions/{revision_id}` until `extraction.status` is `succeeded`, `failed`, or `skipped`.
4. Upload optional supporting sources. Every supporting source must include the BRD's `brd_source_id`.
5. Create a frozen package with `POST /api/v1/projects/{project_id}/manifests`.
6. Start Requirements Analysis with `POST /api/v1/packages/{package_id}/requirements/analyze`.
7. Review the resulting contract with `POST /api/v1/requirement-analyses/{analysis_id}/review`.
8. Use the approved contract as the input to the future Test Case Generation step.

## API endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/health` | Service health |
| POST | `/api/v1/projects` | Create a project |
| GET | `/api/v1/projects` | List projects |
| GET | `/api/v1/projects/{project_id}` | Get project details |
| POST | `/api/v1/projects/{project_id}/sources` | Upload a BRD or supporting source |
| GET | `/api/v1/projects/{project_id}/sources` | List sources and extraction status |
| GET | `/api/v1/revisions/{revision_id}` | Read extraction and fragments |
| GET | `/api/v1/sources/{source_id}/revisions` | List all revisions for a logical source |
| GET | `/api/v1/revisions/{revision_id}/compare/{other_revision_id}` | Compare two revisions with a unified diff |
| POST | `/api/v1/sources/{source_id}/active-revision` | Select a successfully extracted active revision |
| GET | `/api/v1/sources/{source_id}/parent-brd` | Find a supporting source's parent BRD |
| POST | `/api/v1/projects/{project_id}/manifests` | Create an immutable source package |
| GET | `/api/v1/projects/{project_id}/manifests` | List package versions |
| GET | `/api/v1/manifests/{package_id}` | Read exact package revisions |
| POST | `/api/v1/packages/{package_id}/requirements/analyze` | Generate a Requirements Analysis version |
| GET | `/api/v1/packages/{package_id}/requirements` | List analysis versions |
| GET | `/api/v1/requirement-analyses/{analysis_id}` | Read an analysis contract |
| POST | `/api/v1/requirement-analyses/{analysis_id}/review` | Record approval/rejection and review metadata |
| POST | `/api/v1/manifests/{package_id}/lifecycle` | Move a package to ready, invalidated, or published |
| GET | `/api/v1/ingestion-runs/{run_id}` | Read run configuration and provenance |

## Upload example

```powershell
curl.exe -X POST "http://localhost:8001/api/v1/projects/demo/sources" `
  -F "file=@C:\path\to\requirements.docx" `
  -F "source_type=brd" `
  -F "owner=Product" `
  -F "owner_version=1.0"
```

Supported `source_type` values:

```text
brd, campaign_rules, api_spec, database_schema, message_template, ui_description
```

Upload responses use `202` while extraction is pending. Validation failures return `415`, `413`, or `422`. Duplicate content is retained as a traceable revision.

## Requirements Analysis contract

The analysis input is the exact package selected by `package_id`, including its source revisions and extracted fragments. The output contains:

- Functional requirements
- Business rules
- Acceptance criteria
- Actors and entities
- Flow steps and expected outcomes
- Source references
- Ambiguities and conflicts
- Readiness gaps and accepted assumptions
- Analysis version and reviewer decision metadata

The PBM refill-reminder POC splits the BRD into functional requirements such as member identification, prescription eligibility, SMS communication, phone fallback, communication outcomes, and eligibility revalidation.

## Test

From `backend/testiq-poc/backend`:

```powershell
uv run pytest tests/test_source_package.py -q -p no:cacheprovider
```

The tests cover upload validation, extraction fallback, duplicate and changed revisions, source relationships, package manifests, and Requirements Analysis review.

## Troubleshooting

### PostgreSQL connection failure

Confirm PostgreSQL is running, the role/database exist, and `DATABASE_URL` uses the correct host, port, user, password, and database.

### Extraction remains pending

Check the Uvicorn terminal. If `OPENAI_API_KEY` is missing or the model call fails, local extraction should run automatically. Inspect `GET /api/v1/revisions/{revision_id}` for `validation_messages` and `extraction.status`.

### Requirements Analysis is not ready

Confirm the package exists, all package revisions have `extraction.status=succeeded`, and the request uses the package ID returned by manifest creation.

### Frontend cannot reach the backend

Start the backend on port `8001`. The Vite development proxy forwards `/api` requests to `http://127.0.0.1:8001`.
