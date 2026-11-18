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

- Baseline: 33 existing tests passed before editing. All 33 remain, and all 5 new HTTP integration tests pass.
- Browser: Chrome verified the welcome page, demo tour, search, switching roles, approval, booking, receipt entry, claim review, statement import and reconciliation, simulated payment, Admin settings, and reset. The 390 px mobile viewport had no horizontal document overflow.
- Release: JavaScript syntax and Compose configuration were checked. The source ZIP was verified for checksum integrity, extraction and standalone execution.
- Data review: The workspace contains no Git repository or history to inspect. The pre-change snapshot, previous ZIPs, historical policy source-review notes, local database files and environment settings are excluded from the deliverable. A public ZIP allowlist and credential-pattern scan complement the fictional seed review.

## Deployment status

Railway configuration is included for remote Docker builds with a required writable volume at `/data`, secure cookies, and a startup health check. Deploy with one replica. A restart of the extracted application confirmed that the selected role and new trip persisted for the correct visitor, the second visitor retained the seed, and reset touched only the initiating visitor. Deployment requires a Railway account session. The allowlisted ZIP can also serve as a clean GitHub publication source after account authorization.

The application has been exercised over loopback with temporary fictional SQLite files. Docker built an earlier v3 image, then its engine returned an internal error on final rebuild attempts. The final container and Caddy runtime smoke checks are pending. Public HTTPS deployment requires an authorized Docker host and public DNS hostname. No public HTTPS URL has been verified.
