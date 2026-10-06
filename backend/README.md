# TestIQ Backend

FastAPI service for BRD ingestion, source-package creation, and Requirements Analysis.

## What this service does

- Accepts BRD files and supporting sources (`.docx`, `.pdf`, `.txt`, `.md`, `.json`, `.yaml`, `.yml`, `.sql`, `.csv`). JSON is syntax-validated; other structured sources are preserved and extracted as UTF-8 text.
- Preserves original files and immutable source revisions.
- Extracts traceable fragments with paragraph, table, page, or line references.
- Uses OpenAI file extraction for BRDs and supporting sources, with local extraction as a documented fallback when the OpenAI call fails.
- Creates immutable source-package manifests.
- Extracts BRD business rules for explicit approve/reject review before indexing.
- Maintains an immutable, project-level Knowledge Base version for every reviewed resource package.
- Reuses unchanged chunks, embeddings, and graph facts across compatible versions; only new or changed content is reprocessed.
- Uses hybrid vector, keyword, and graph retrieval with reciprocal-rank fusion and semantic reranking.
- Generates versioned Requirements Analysis contracts from the selected package.
- Supports reviewer decisions, comments, accepted assumptions, ambiguity/conflict findings, and readiness status.

Downstream LLM generation is grounded with the package-pinned Knowledge Base version. Retrieval selects evidence; the LLM analyzes and structures that evidence.

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
# Shared PostgreSQL server.
TESTIQ_POSTGRES_HOST=10.73.1.56
TESTIQ_POSTGRES_PORT=5432
TESTIQ_POSTGRES_DATABASE=testiq
TESTIQ_POSTGRES_USER=testiqdev
TESTIQ_POSTGRES_PASSWORD=
# DATABASE_URL=postgresql://testiqdev:<password>@10.73.1.56:5432/testiq
TESTIQ_DATA_DIR=./data
TESTIQ_DB_PATH=./data/testiq.db
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4o-mini
OPENAI_TIMEOUT_SECONDS=90
OPENAI_PROMPT_CACHING_MODE=disabled
TESTIQ_PGVECTOR_MODE=auto
TESTIQ_POSTGRES_LOCK_TIMEOUT_MS=5000
TESTIQ_POSTGRES_STATEMENT_TIMEOUT_MS=90000
MAX_UPLOAD_BYTES=26214400
# REQUIRED_BRD_SECTIONS=Scope,Requirements,Acceptance Criteria
```

`DATABASE_URL` overrides the individual `TESTIQ_POSTGRES_*` values. If neither `DATABASE_URL` nor `TESTIQ_POSTGRES_HOST` is set, the service uses local SQLite at `TESTIQ_DB_PATH`. The application creates the `testiq` schema tables at startup. Original uploads are stored under `TESTIQ_DATA_DIR/uploads`.

`TESTIQ_PGVECTOR_MODE` supports `auto`, `disabled`, and `required`. `auto` uses pgvector when the server provides it and otherwise uses the PostgreSQL-backed Python cosine fallback. `disabled` explicitly selects that fallback; embeddings remain persisted in PostgreSQL, keyword retrieval preselects a broad vector candidate pool, and cosine scoring, graph expansion, reciprocal-rank fusion, and semantic reranking remain active. `required` stops startup when pgvector cannot be enabled.

### OpenAI prompt-caching cost control

Prompt caching applies to input tokens, not generated output tokens. Cached reads are normally cheaper, but cache writes for GPT-5.6+ can cost more. Caching also requires a repeated prompt prefix; exceeding 1,024 tokens alone is not sufficient.

The current TestIQ `.env` uses `gpt-4o-mini`, not GPT-5.6, so GPT-5.6 explicit-cache controls do not apply directly to that model. TestIQ defaults `OPENAI_PROMPT_CACHING_MODE=disabled`: it adds a unique per-request marker to prevent repeated prompts from reusing the cache, and applies explicit-only mode when GPT-5.6+ is configured. Set the mode to `explicit` to use GPT-5.6+ explicit-only caching, or `implicit` to retain the provider default. Refer to the [OpenAI Prompt Caching documentation](https://developers.openai.com/api/docs/guides/prompt-caching) for current model behavior and pricing.

The shared database already has the `testiq` database and `testiq` schema. To create or update the TestIQ control-plane tables in that schema without starting the API, run:

```powershell
uv run python scripts/init_testiq_schema.py
```

## Run

From this directory:

```powershell
uv run python -m uvicorn app.main:app --reload --reload-dir app --env-file .env --host 127.0.0.1 --port 8001
```

Use `--reload-dir app` during development. It prevents test files, temporary data, and generated artifacts from triggering backend reloads and avoids interrupted child-process imports during rapid file changes.

Endpoints:

- Health: <http://localhost:8001/health>
- Swagger: <http://localhost:8001/docs>
- OpenAPI: <http://localhost:8001/openapi.json>

Expected health response:

```json
{ "status": "ok", "service": "source-package" }
```

## User workflow

1. Create a project, or let the frontend create one during the first BRD upload.
2. Upload a BRD with `POST /api/v1/projects/{project_id}/sources`.
3. Poll `GET /api/v1/revisions/{revision_id}` until `extraction.status` is `succeeded`, `failed`, or `skipped`.
4. Upload optional supporting sources. Every supporting source must include the BRD's `brd_source_id`.
5. Create a frozen package with `POST /api/v1/projects/{project_id}/manifests`.
6. Review all extracted business rules with `POST /api/v1/packages/{package_id}/business-rules/review`.
7. Build the next persistent Knowledge Base version with `POST /api/v1/packages/{package_id}/knowledge-index`.
8. Generate Business Use Cases and Functional Requirements from retrieved knowledge.
9. Review the resulting requirements and continue to RAG-grounded Test Case and Test Data generation.

## API endpoints

| Method | Endpoint                                                      | Purpose                                            |
| ------ | ------------------------------------------------------------- | -------------------------------------------------- |
| GET    | `/health`                                                     | Service health                                     |
| POST   | `/api/v1/projects`                                            | Create a project                                   |
| GET    | `/api/v1/projects`                                            | List projects                                      |
| GET    | `/api/v1/projects/{project_id}`                               | Get project details                                |
| POST   | `/api/v1/projects/{project_id}/sources`                       | Upload a BRD or supporting source                  |
| GET    | `/api/v1/projects/{project_id}/sources`                       | List sources and extraction status                 |
| GET    | `/api/v1/revisions/{revision_id}`                             | Read extraction and fragments                      |
| GET    | `/api/v1/sources/{source_id}/revisions`                       | List all revisions for a logical source            |
| GET    | `/api/v1/revisions/{revision_id}/compare/{other_revision_id}` | Compare two revisions with a unified diff          |
| POST   | `/api/v1/sources/{source_id}/active-revision`                 | Select a successfully extracted active revision    |
| GET    | `/api/v1/sources/{source_id}/parent-brd`                      | Find a supporting source's parent BRD              |
| POST   | `/api/v1/projects/{project_id}/manifests`                     | Create an immutable source package                 |
| GET    | `/api/v1/projects/{project_id}/manifests`                     | List package versions                              |
| GET    | `/api/v1/manifests/{package_id}`                              | Read exact package revisions                       |
| GET    | `/api/v1/packages/{package_id}/business-rules`                 | List source-derived business rules                 |
| POST   | `/api/v1/packages/{package_id}/business-rules/review`          | Save and verify rule decisions                     |
| POST   | `/api/v1/packages/{package_id}/knowledge-index`                | Build an immutable Knowledge Base version          |
| GET    | `/api/v1/packages/{package_id}/knowledge-indexes`              | List package Knowledge Base indexes                |
| GET    | `/api/v1/projects/{project_id}/knowledge-versions`             | List project Knowledge Base history                |
| POST   | `/api/v1/packages/{package_id}/requirements/analyze`          | Generate a Requirements Analysis version           |
| GET    | `/api/v1/packages/{package_id}/requirements`                  | List analysis versions                             |
| GET    | `/api/v1/requirement-analyses/{analysis_id}`                  | Read an analysis contract                          |
| POST   | `/api/v1/requirement-analyses/{analysis_id}/review`           | Record approval/rejection and review metadata      |
| POST   | `/api/v1/manifests/{package_id}/lifecycle`                    | Move a package to ready, invalidated, or published |
| GET    | `/api/v1/ingestion-runs/{run_id}`                             | Read run configuration and provenance              |

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

## Test data and Bronze records

Approved test cases use the versioned CloudEvent contract at `backend/app/templates/pharmacy_sample_notification_v1.json`. `POST /api/v1/test-case-generations/{generation_id}/test-data` requires a ready package Knowledge Base index and a working OpenAI configuration; the LLM generates case-specific synthetic values from the approved case and retrieved evidence. There is no local data-generation fallback. TestIQ assigns unique event IDs, correlation IDs, timestamps, member resource IDs, and safe synthetic phone values.

Generated events are checked against the exact template shape and types, populated-field rules, unique identifiers, safe phone format, and supported eligibility and SMS-consent scenario expectations. Reviewers can edit the draft JSON. Before insertion, TestIQ reassigns its trace metadata and validates the reviewed event again. `POST /api/v1/test-case-generations/{generation_id}/test-data/save` stores the intact CloudEvent envelope and original JSON bytes in `bronzebox.raw_file`; the downstream Bronze-to-Silver process owns splitting and Silver projection. `testiq.test_data_bronze_ref` records the raw-file ID for traceability. Execution checks that the reviewed event matches the Bronze payload; it does not execute the event through the application under test.

## Test

From `backend/testiq-poc/backend`:

```powershell
uv run pytest tests/test_source_package.py -q -p no:cacheprovider
```

The tests cover upload validation, extraction fallback, duplicate and changed revisions, source relationships, package manifests, and Requirements Analysis review.

## Troubleshooting

### PostgreSQL connection failure

Confirm PostgreSQL is running, the role/database exist, and `DATABASE_URL` uses the correct host, port, user, password, and database.

### pgvector is unavailable

Set `TESTIQ_PGVECTOR_MODE=disabled` temporarily. This keeps PostgreSQL as the system of record and uses Python cosine similarity for vector scoring. After a database administrator installs pgvector and runs `CREATE EXTENSION IF NOT EXISTS vector`, set the mode back to `auto` and restart the backend.

### Extraction remains pending

Check the Uvicorn terminal. If `OPENAI_API_KEY` is missing or the model call fails, local extraction should run automatically. Inspect `GET /api/v1/revisions/{revision_id}` for `validation_messages` and `extraction.status`.

### Requirements Analysis is not ready

Confirm the package exists, all package revisions have `extraction.status=succeeded`, and the request uses the package ID returned by manifest creation.

### Frontend cannot reach the backend

Start the backend on port `8001`. The Vite development proxy forwards `/api` requests to `http://127.0.0.1:8001`.
