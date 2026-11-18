# Delivery verification

## Implementation

| Files | Changes |
| --- | --- |
| `server.py`, `demo.py` | Anonymous visitor sessions, server-selected persona and permission checks, atomic reset, expiry and storage bounds, sample scenarios, metrics, secure cookie configuration, and request logging. |
| `workflow.py`, `policies.py` | Manager rejection, public Admin configuration, policy violation explanations, optional fictional nightly caps, and continued support for existing flat policy snapshots. |
| `public/index.html`, `public/style.css`, `public/app.js` | Responsive navigation and dashboard, filters and pagination, action dialogs, feedback, reset, and a six-step demo tour. |
| `tests/test_demo.py`, `tests/test_workflow.py`, `tests/test_package.py` | New HTTP coverage, migration of the existing offline HTTP fixture to cookie sessions, and expanded public archive exclusion checks. |
| `.env.example`, `Dockerfile`, `.dockerignore`, `compose.yaml`, `Caddyfile` | Non-root container, persistent SQLite volume, HTTPS proxy configuration, and health check. |
| `README.md`, `docs/ARCHITECTURE.md`, `docs/screenshots/` | English portfolio documentation, Mermaid architecture and database diagrams, a five-minute route, résumé bullets, and six browser captures. |
| `.gitignore`, `scripts/package_public.py` | Source allowlist, private-file exclusions, and portable archive verification. |

Public policy administration is assigned to Admin. The legacy workflow module continues to accept manager policy actions for existing callers, while the public HTTP layer enforces Admin access. Public sessions always initialize from fictional fixtures and use a separate store from any legacy local database.

## Verification

- Tests: All 38 existing v3 tests pass, plus 3 Render-mode tests for startup seeding, lost session recovery, and concurrent visitor isolation.
- Browser: Chrome verified the welcome page, demo tour, search, switching roles, approval, booking, receipt entry, claim review, statement import and reconciliation, simulated payment, Admin settings, and reset. The 390 px mobile viewport had no horizontal document overflow.
- Release: JavaScript syntax and Compose configuration were checked. The source ZIP was verified for checksum integrity, extraction and standalone execution.
- Data review: GitHub publication starts from the audited allowlist. The pre-change snapshot, previous ZIPs, historical policy source-review notes, local database files and environment settings are excluded. A credential-pattern scan complements the fictional seed review.

## Deployment status

The [public Render Free service](https://travel-desk-demo.onrender.com) uses one free native Python instance. The service provisions neither a database nor a disk. External HTTPS checks covered all four roles, a complete request-to-settlement flow, Secure cookies, visitor isolation, and reset. Chrome verified the deployed workflow and 390 px mobile layout. Render may discard visitor edits when the instance sleeps, restarts or is redeployed.

Railway configuration is included for remote Docker builds with a required writable volume at `/data`, secure cookies, and a startup health check. Deploy with one replica. A restart of the extracted application confirmed that the selected role and new trip persisted for the correct visitor, the second visitor retained the seed, and reset touched only the initiating visitor. Deployment requires a Railway account session. The allowlisted ZIP can also serve as a clean GitHub publication source after account authorization.

The separate Docker Compose and Caddy configuration remains available for self-hosting. This release validation used Render's native Python runtime.
