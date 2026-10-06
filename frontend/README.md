# TestIQ Frontend

React + Vite frontend for the TestIQ source-package and Requirements Analysis POC.

## Features

- Project workspace navigation
- BRD upload with supporting-source registration
- Source-package review and creation
- Business-rule review with individual approve/reject, Approve All, save verification, and build progress
- Persistent project Knowledge Base history with incremental reuse counts
- Hybrid retrieval verification against an immutable package-pinned version
- Extraction progress and validation feedback
- Requirements Analysis version selection
- Requirement, business-rule, acceptance-criteria, and expected-outcome review
- Selective approval with reviewer comments and accepted assumptions

The Knowledge Base and RAG flow are part of the main journey and use the existing Phase 3 UI system.

## Prerequisites

- Node.js 18+ and npm
- TestIQ backend running on `http://127.0.0.1:8001`

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
http://127.0.0.1:8001
```

If the backend uses another URL, set `testiq_base_url` in browser local storage or update `vite.config.js` for local development.

## Use the application

1. Open **Sources**.
2. Upload a BRD and wait for extraction to finish.
3. Add optional supporting sources such as campaign rules, API specifications, database schemas, message templates, or UI descriptions.
4. Select **Review package**, then create the immutable source package.
5. Open **Knowledge Base**, approve or reject every extracted rule, and save the decisions.
6. Wait for the package-pinned Knowledge Base version to finish updating.
7. Continue to **Business Use Cases** and **Functional Requirements**.
8. Review requirements, then continue to Test Cases and Test Data.

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
  components/KnowledgeBasePage.jsx Rule review, incremental KB versions, and retrieval verification
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
GET  /api/v1/packages/{package_id}/business-rules
POST /api/v1/packages/{package_id}/business-rules/review
POST /api/v1/packages/{package_id}/knowledge-index
GET  /api/v1/projects/{project_id}/knowledge-versions
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
