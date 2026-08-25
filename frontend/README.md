# ProofGuard Executive Dashboard

Desktop-first React presentation and observability UI for the existing ProofGuard FastAPI protocol. The backend remains authoritative: this application fetches, formats, joins, and displays existing records; it does not implement routing, clustering, consensus, scoring, reward, or accounting formulas.

## Stack

- React 19 + TypeScript + Vite
- React Router
- TanStack Query for API state, caching, refresh, and active-audit polling
- Recharts for the small network composition visualization
- Vitest + Testing Library for focused protocol/presentation mapping tests
- ESLint with TypeScript and React Hooks rules
- Plain centralized CSS design tokens; no UI framework or runtime styling dependency

## Visual system

The interface uses a dark-first “security intelligence console” system: restrained blue is the product accent, while green, amber, red, and neutral tones are reserved for protocol meaning. Shared primitives centralize surfaces, typography, spacing, entity icons, status/severity badges, technical identifiers, and unavailable/loading/error states.

FindingCluster membership and validation truth are always rendered separately. A frozen/finalized cluster is an immutable validator work unit; it is not displayed as a confirmed vulnerability unless the backend exposes a confirmed/accepted resolution. Consensus thresholds, reward amounts, and accounting verification are rendered from backend records and are never recalculated by the UI.

## Install and run

Backend terminal:

```bash
cd apps/audit-api
source .venv/bin/activate
uvicorn app.main:app --reload --port 8000
```

Frontend terminal:

```bash
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173`.

## Backend URL configuration

The development default is `http://127.0.0.1:8000`. Override it with:

```bash
cp .env.example .env
# Edit VITE_API_BASE_URL in .env
```

The API allows the two Vite development origins by default. Override the backend list with the comma-separated `AUDIT_API_CORS_ORIGINS` setting.

## Commands

```bash
npm run dev       # development server
npm test          # focused component tests
npm run test:watch
npm run typecheck # TypeScript project check
npm run lint      # TypeScript/React static analysis
npm run build     # type-check and production build
npm run preview   # serve the production build locally
```

The production output is written to `frontend/dist/`.

## Routes

- `/` — executive network and protocol overview
- `/audits` — project/routing audit registry
- `/audits/:projectId/:routingId` — full end-to-end audit detail
- `/audits/:projectId/:routingId/present` — directly addressable Presentation Mode
- `/findings` — cross-audit FindingCluster index
- `/nodes` and `/nodes/:nodeId` — node registry and separate agent/validator history
- `/rewards` — selected audit TaskRewardBudget and separate reward streams

Presentation Mode supports Previous/Next buttons, stage dots, Left/Right or PageUp/PageDown keys, and Escape to return to Audit Detail. “Demo Audit” is a frontend-only local-storage preference; it never mutates protocol state.

## Architecture

`src/api/client.ts` is the only network layer. It contains the base URL, typed requests, stable query keys, audit-index composition, the full audit bundle loader, and node-detail composition. Pages and components do not issue raw `fetch` calls.

`src/types/protocol.ts` contains the subset of OpenAPI response types used by the UI. Decimal protocol values remain strings until formatting so the frontend does not change economic precision.

`src/components/` contains reusable status/loading/error primitives and domain views for routing, submissions, clusters, committees, independent reproductions, attestations, consensus, disputes, quality, rewards, accounting, and role-separated node performance.

Primary resources (`Project` and `ProjectRoutingRecord`) fail the page visibly if unavailable. Optional downstream resources fail independently, render “Data unavailable,” and are listed in a data-availability disclosure instead of blanking the page or fabricating zeroes. Audit Detail polls every four seconds only while an attached AuditRun has a non-terminal backend status; finalized/completed/failed screens do not poll.

## API assumptions and backend addition

The UI was mapped from the generated FastAPI OpenAPI schema. It consumes existing read endpoints for:

- projects, scopes, AuditRuns, agent executions, and routing plans;
- nodes, agent CategoryScores/category performance, and validator-only performance, ValidatorCategoryScore, and ValidatorMembership;
- submissions and FindingClusters;
- validator committees, assignments, reproductions, ValidationAttestations, ValidationConsensus, disputes, and ValidationQualityAssessments;
- TaskRewardBudget, finding/operator reward calculations, Week 7 miner cycles/events/verifications, and validator cycles/allocations/verifications.

One read-only endpoint was added: `GET /projects`. The backend previously exposed only `GET /projects/{project_id}`, which made audit discovery impossible without known IDs. It returns existing public `ProjectMetadataResponse` fields ordered newest first and performs no protocol mutation or calculation. Configurable CORS middleware was also added so Vite can consume the API from its separate development origin.

Some backend records are intentionally absent in early/partial audits. In particular, a project may have no routing, a routing may have no clusters or committees, and reward verification may not exist before finalization. The UI reports those gaps rather than substituting fixtures. The cluster API exposes canonical IDs and the root-cause key but not a separate canonical finding title/body read model, so the UI uses the root-cause key and identifiers without reading backend storage files. Current score endpoints do not expose a general score-snapshot time series; node detail therefore shows current scores plus event-backed counters rather than an invented chart.

## Tests

The focused suite covers:

- loading and API error states;
- end-to-end audit pipeline rendering;
- authoritative versus shadow committee treatment;
- backend `DISPUTED` rendering for 3 ACCEPT / 2 REJECT with threshold 4;
- escalation and cumulative minority-alignment explanation;
- separate hybrid agent and validator score sections;
- separate miner and validator reward streams;
- backend-driven accounting PASS/FAIL and unavailable-data handling.
- frozen cluster membership versus unresolved validation truth;
- confirmed versus rejected status mapping;
- keyboard-operable, read-only Presentation Mode navigation.

No normal application route contains mock protocol statistics. Test fixtures live only under `src/test/`.
