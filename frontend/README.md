# TestIQ Frontend

React + Vite frontend for the TestIQ source-package and Requirements Analysis POC.

## Features

- Project workspace navigation
- BRD upload with supporting-source registration
- Source-package review and creation
- Extraction progress and validation feedback
- Requirements Analysis version selection
- Requirement, business-rule, acceptance-criteria, and expected-outcome review
- Selective approval with reviewer comments and accepted assumptions

Knowledge Fabric/GraphRAG is intentionally not included in this POC.

## Prerequisites

- Node.js 18+ and npm
- TestIQ backend running on `http://127.0.0.1:8000`

## Install

From `frontend/testiq-poc/frontend`:

```powershell
npm install
```

## Run in development

Start the backend first by following the backend README. Then, from this directory:

```powershell
npm run dev
```

Open the URL shown by Vite, normally <http://localhost:5173>.

The Vite proxy forwards requests beginning with `/api` to:

```text
http://127.0.0.1:8000
```

If the backend uses another URL, set `testiq_base_url` in browser local storage or update `vite.config.js` for local development.

## Use the application

1. Open **Sources**.
2. Upload a BRD and wait for extraction to finish.
3. Add optional supporting sources such as campaign rules, API specifications, database schemas, message templates, or UI descriptions.
4. Select **Review package**, then create the immutable source package.
5. Open **Requirements**.
6. Select the package and run a new Requirements Analysis.
7. Expand requirements to review only their related rules, acceptance criteria, and expected outcomes.
8. Select the items needed for the journey and submit the reviewer decision.

## Scripts

```powershell
npm run dev       # Start the Vite development server
npm run build     # Create a production build in dist/
npm run preview   # Serve the production build locally
npm run lint      # Run oxlint
```

## Project structure

```text
src/
  api/client.js                  Backend API client
  components/UploadStep.jsx      BRD and source-package workflow
  components/RequirementsPage.jsx Requirements Analysis review UI
  App.jsx                        Workspace shell and navigation
  App.css                        Workspace styling
```

## Backend contract

The frontend expects the backend described in `backend/testiq-poc/backend/README.md`.
The important flow is:

```text
POST /api/v1/projects/{project_id}/sources
GET  /api/v1/revisions/{revision_id}
POST /api/v1/projects/{project_id}/manifests
POST /api/v1/packages/{package_id}/requirements/analyze
GET  /api/v1/packages/{package_id}/requirements
POST /api/v1/requirement-analyses/{analysis_id}/review
```

## Verify the frontend

Run:

```powershell
npm run build
npm run lint
```

If the page is stale after backend or frontend changes, refresh the browser and restart `npm run dev` if Vite did not reload the changed module.
