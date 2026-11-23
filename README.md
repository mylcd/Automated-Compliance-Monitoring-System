# MYL Inc. Compliance Monitoring

SJSU CMPE 272 final project: a local compliance monitoring demonstration for MYL Inc.'s restaurant loyalty program (GDPR) and payment systems (PCI DSS).

## Current scope

Phases 1–5 provide the local application, General Manager login, inventory, ERP/loyalty/payment evidence, Resource Planning, manual and scheduled assessments, ten checks, immutable history, findings, controlled mock corrections, verified remediation, audit history, filtered dashboards, and YAML exports. Restaurants remain **not assessed** until scanned.

All operational data is fictional. Exactly five restaurants (`R001`–`R005`) are seeded, with one General Manager and two Managers plus ten Staff per restaurant: 61 employees total. October 2026 is the only seeded month. There are exactly five visit seed files, one per restaurant/month, with 10–20 visits on each weekday and 30–45 on each Saturday/Sunday. The fixed random seeds currently generate 3,375 visits. Amounts are USD. This is a local demonstration with no external enterprise service dependencies.

## Start locally with Docker

Prerequisites: Docker Desktop (or Docker Engine with Compose), and Python 3 to generate local configuration. Start Docker before running Compose. Initial dependency and container-image downloads need internet access; the running application uses local services and bundled assets only.

From the repository root:

```sh
python3 scripts/configure_local.py
docker compose up --build -d --wait
```

The configuration command creates a private, ignored `.env` with username `admin` and a randomly generated password. It preserves any existing `.env`. Open `.env` locally to view the credentials, then sign in at [http://localhost:8080](http://localhost:8080). No credentials are committed or printed by the configuration script.

After the initial build, start with `docker compose up -d --wait`. Use `docker compose logs` to inspect startup, and `docker compose down` to stop. Database and evidence files remain in the host directories across container removal and recreation.

Compose defaults `SEED_DEMO_DATA=true`: on an empty restaurant inventory, startup imports the bundled fixtures once. Existing data and credentials are preserved on later starts. The existing administrator represents the General Manager and can read all restaurants and employee/payroll data. Manager and Staff employees have no platform accounts.

The app binds to the local loopback interface. Open it using `localhost` as shown above. If you change the port, update both `WEB_PORT` and `APP_ORIGIN` in `.env` to match, then recreate the containers.

## Architecture

```text
Browser -> localhost:8080 -> Nginx / React assets
                              |
                              +-- /api/* -> FastAPI -> SQLite
                                                   -> local evidence directory
```

The API has no published host port in Compose. The frontend reverse proxy keeps browser API requests on the same origin. A single API worker is intentional for the local SQLite application and serial scan executor.

| Path | Purpose |
|---|---|
| `frontend/` | React, TypeScript, Vite, Material UI, Nginx |
| `backend/app/` | FastAPI application, configuration, authentication, models |
| `backend/migrations/` | Alembic database migrations |
| `backend/tests/` | Isolated backend integration tests |
| `backend/seed_data/` | Independent fictional ERP, loyalty, and payment source JSON plus deterministic generator |
| `data/myl.db` | Generated SQLite database; ignored by Git |
| `data/mock_sources/` | Versioned runtime copies for demo corrections; ignored by Git |
| `evidence/runtime/` | Generated local evidence; ignored except directory placeholder |
| `.env.example` | Documented configuration template |

Project memory and planning are maintained outside this repository.

## Isolated presentation copy

Prepare a separate local demonstration with the same application and private administrator credentials:

```sh
python3 scripts/demo.py start
```

Open [http://127.0.0.1:8081](http://127.0.0.1:8081). This uses Compose project `myl-rehearsal`, its own images/network and generated `../phase6_demo/` data/evidence/source directories. Its hostname also separates browser cookies from the primary `localhost:8080` app. The generated Compose file contains environment references, not copied credentials. First startup seeds all five restaurants; later starts preserve its history. Use `start --build` after application source changes.

The following commands operate only on that isolated copy:

```sh
python3 scripts/demo.py status
python3 scripts/demo.py reset --yes
python3 scripts/demo.py add-loy05
python3 scripts/demo.py prepare-feed
python3 scripts/demo.py tls-off
python3 scripts/demo.py tls-on
python3 scripts/demo.py stop
```

`reset --yes` disables scheduling, waits for an active scan to finish, clears the disposable presentation history/data and imports the presentation fixture including R001/LOY-05; it preserves its login credentials/sessions. `start` also ensures that extra case exists. `add-loy05` adds it to an existing presentation copy without resetting history or duplicating the event. It records Manager E001-01 successfully updating C001-02 after their update approval expired; their current approval is read-only. Run a GDPR assessment to detect it. Pending connector edits must be collected or reconciled first.

The presentation baseline has four findings and **46/50 passes (92%)**. Consent and TLS corrections raise it to **94%**, then **96%**; LOY-05 and R003 logging remain open. The primary app and default repository fixtures retain their original three-failure baseline. `prepare-feed` initializes its editable connector from current imports if absent. `tls-off`/`tls-on` change only the R002 mock payment TLS setting in that connector, with a new source capture/version; the next enabled scheduled scan collects the change. `stop` removes its containers/network and retains files for another start.

With the copy running, rehearse clean start, 92% → 94% → 96%, reporting, actual scheduled recurrence/resolution, interrupted-scan restart/retry, logout/denial and baseline reset/repeat:

```sh
.venv/bin/python scripts/smoke_phase6.py --reset-demo
```

This explicitly resets only the isolated copy and takes about three minutes because it waits for real scheduled intervals. Verification JSON and stage YAML exports are retained under `../phase6_demo/rehearsal-artifacts/`. The full presentation script, screenshots, project memory and planning stay outside the repository.

## Mock integrations and demo data

There is no detailed ERP application. `backend/seed_data/erp.json`, `loyalty.json`, and `payment.json` provide supporting source snapshots. Visit records exist only in the five separate files `backend/seed_data/visits/R001-2026-10.json` through `R005-2026-10.json`; there is no combined visit seed file. The loader joins them in memory for validation/import. The connector validates types, IDs, daily visit ranges, source relationships, staffing limits, visit-payment reconciliation, and payroll allocations before committing imports. It writes immutable JSON evidence under `evidence/runtime/phase2/` with source timestamps and SHA-256 hashes. No real card details or passwords are included.

Each visit has `payment_received_minor` (USD cents), `currency`, and a unique `sales_transaction_id`. Each links to one matching posted sale with the same tax-inclusive amount, currency, restaurant, and time. Every payment is exactly three times its original seeded value (now USD 45.00–360.00); visits, dates, and membership data are unchanged. The October seed has no separate refunds or voids, so visit payments sum to **Payment received**, both per restaurant and company-wide. Payments include the configured demo tax of 10%.

Five monthly visit seed files produce **five monthly visit evidence snapshots**, alongside 17 other dataset snapshots. Visits and the Evidence tab's Visits dataset show **155 daily rows** (5 restaurants × 31 days), such as `V-R001-2026-10-01`. Daily rows show visit/member counts and payment totals. Clicking Evidence opens that restaurant's full October snapshot, scrolls to the selected day's records, and highlights them. Download JSON still returns the complete monthly snapshot.

Re-importing unchanged data adds no duplicate records or snapshots. Changed valid sources retain prior evidence. Historical approvals, consent events, visits, sales, and employee action logs cannot be silently overwritten; append new source IDs for new historical facts. An import that omits existing records is rejected rather than silently retaining misleading stale state. For fresh baseline/scenario comparisons, explicitly reset the local demo data first.

From `backend/`, using native development dependencies:

```sh
../.venv/bin/python -m app.seed check
../.venv/bin/python -m app.seed import
```

For the running Compose database, use the same commands inside the backend:

```sh
docker compose exec backend python -m app.seed check
docker compose exec backend python -m app.seed import
```

Three scenarios are supported with `--scenario baseline` (default), `--scenario remediated`, and `--scenario unauthorized-modification`. The baseline has missing marketing consent at R001, disabled payment TLS at R002, and disabled logging at R003. The remediated fixture supplies the missing consent evidence and enables TLS, leaving logging unchanged. The separate action scenario appends a successful customer update by a read-only Manager. Phase 3 evaluates these facts when the General Manager starts an assessment.

To clear **generated demo domain records and their managed evidence files** in the local Compose database, preserving the administrator, credentials, login sessions, and unrelated files:

```sh
docker compose exec backend python -m app.seed reset --yes
docker compose exec backend python -m app.seed import --scenario baseline
```

Reset removes generated assessment/finding/audit history, runtime source revisions, imported data, and managed evidence. It refuses while an assessment is queued or running and preserves unrelated files and login credentials/sessions. A later backend restart with automatic seeding enabled re-imports the baseline if inventory is empty. Snapshot changes and failed imports do not mutate the independent source files. Regenerate committed fixtures only when intentionally changing the seed specification: `python3 backend/seed_data/generate.py`.

The Resource Planning Summary defaults to October 2026. Payment received is **USD 685,881.72**, including 10% tax. **Total Sales excludes tax: payment ÷ 1.10**, rounded half up to cents for each transaction and then summed: **USD 623,529.13**. Tax is the receipt minus the rounded net amount: **USD 62,352.59**. Summing per-transaction amounts keeps restaurant and company totals consistent; dividing the aggregate payment may differ slightly due to rounding. Posted refunds reduce gross receipts, net sales, and tax; voids are excluded. The API returns `total_payments_minor`, `total_sales_minor`, and `total_tax_minor` separately. Total Payroll Amount is allocated monthly salary plus allowance (USD 228,750.00); the General Manager is split equally across five restaurants and counted once company-wide. Currency totals remain separate. Missing/unseeded months show unavailable amounts, not zero. Monthly payroll amounts do not confirm payment. Replacing older seed values requires an explicit demo reset/import after backing up the configured database/evidence; migrations alone preserve historical records rather than silently changing them.

## Authentication and configuration

Administrator credentials initialize the database on first startup. Passwords use Argon2 hashes. Sessions use random HttpOnly, SameSite cookies, with only token hashes stored in SQLite. Sessions expire after `SESSION_HOURS` (default 8); logout revokes the server-side session. Mutating browser requests require same-origin headers; logout and assessment start/retry also require the session's CSRF token. Credentials and session cookies are never placed in browser local storage.

For this local demo, passwords must be nonempty (not whitespace-only), must not start with the template marker `replace-`, and must be at most 1024 characters to sign in. There are no uppercase, number, or symbol requirements. The setup script generates a random password for new configurations and preserves existing credentials.

Editing credentials in `.env` does not silently overwrite the existing administrator. To explicitly update credentials and revoke existing sessions:

```sh
# First edit ADMIN_USERNAME / ADMIN_PASSWORD in .env.
docker compose run --rm backend python -m app.bootstrap --reset-password
docker compose up -d --force-recreate --wait
```

`COOKIE_SECURE=false` supports the local HTTP demo. The one-process login throttle allows 10 attempts per minute per client and resets when the API restarts. This setup targets the local demonstration.

## Manual assessments and findings

1. Sign in as the General Manager and open **Assessments**.
2. Select at least one restaurant and framework, then choose **Start assessment**.
3. Inspect progress, the automated-check pass rate, coverage, and result details. Filter results by restaurant, control, or status.
4. Open **Findings** to inspect failed subjects, severity, remediation guidance, occurrence history, and linked evidence snapshots.

Manual scans assess the **current imported evidence**. Scheduled scans first collect the editable local mock connector, then use the same evaluator. Use the CLI connector to import fixture changes, or apply a named correction in Findings. Queuing pins evidence IDs, hashes, scope, timestamps, rules/mappings, evaluation time, and freshness policy. Later imports do not rewrite earlier results. The schema upgrades to `0006` automatically on startup without clearing inventory, payments, credentials, sessions, or earlier assessment/finding history.

The default freshness policy is **30 days**, configurable with `ASSESSMENT_MAX_EVIDENCE_AGE_DAYS` (1–365). This is demo policy, not a legal deadline. The dated October evidence is captured November 1, 2026; assessments after its freshness window deliberately show `unknown`. Missing/incomplete/stale evidence yields `unknown`, malformed or integrity-failed evidence yields `error`, and explicitly unsupported scope yields `not_applicable`. Empty data is never silently interpreted as a passing check. No sensitive card data is inspected or collected.

| Check | Automated behavior | Supporting reference |
|---|---|---|
| LOY-01 | Consent evidence precedes consent-based activity | GDPR 4(11), 6(1)(a), 7(1) |
| LOY-02 | Withdrawal respected until valid re-consent | GDPR 7(3) |
| LOY-03 | Active loyalty records stay within configured retention | GDPR 5(1)(e) |
| LOY-04 | Unresolved customer requests are not past recorded due dates | GDPR 12(3) |
| LOY-05 | Observed permissions and successful actions stay within approvals valid at the relevant time | GDPR 5(1)(f), 32(1)(b), 32(4) |
| PAY-01 | TLS enabled with an allowed protocol | PCI DSS 4.0.1, 4.2.1 |
| PAY-02 | Active accounts meet configured MFA/password policy | PCI DSS 4.0.1, 8.3.6 / 8.4.2 |
| PAY-03 | Audit-logging configuration enabled | PCI DSS 4.0.1, 10.2.1 |
| PAY-04 | Installed patch matches configured required version | PCI DSS 4.0.1, 6.3.3 |
| PAY-05 | Metadata reports no prohibited sensitive authentication-data storage | PCI DSS 4.0.1, 3.3.1 |

Mappings were reviewed against the [official GDPR text](https://eur-lex.europa.eu/eli/reg/2016/679/oj/eng) and PCI SSC's [PCI DSS library](https://www.pcisecuritystandards.org/document_library/?class=pcidss&doc=pci_dss), [patch guidance](https://www.pcisecuritystandards.org/faqs/1597/), and [prohibited-storage guidance](https://www.pcisecuritystandards.org/faqs/1154/). These are limited supporting checks, not a complete regulatory assessment or certification. Check-specific limitations are recorded in the catalog and copied into each result. Consent validity, lawful deadlines, actual security enforcement, patch timeliness, and absence of sensitive data require evidence beyond these mocks.

With fresh baseline evidence, a full assessment produces **50 evaluations: 47 pass, 3 fail (94%)**, with **5/5 restaurant coverage**. The failures are R001/LOY-01, R002/PAY-01, and R003/PAY-03. The separate unauthorized-modification scenario also fails R001/LOY-05; denied updates and historically approved updates do not count as unauthorized modifications. The remediated scenario produces **49 passes / 1 failure (98%)**. Non-member visits do not require loyalty consent merely because they exist.

Pass rate is passes divided by applicable evaluations, including `unknown`/`error` and excluding `not_applicable`. No applicable evaluations or an incomplete run means N/A. Coverage is separate. Repeated assessments update stable target/control findings without duplicates. Shared-system failures create one finding per shared asset/control, with affected restaurant scope. Subject identity separates shared/private histories; customer-specific consent stays restaurant-scoped. Phase 4 resolves only subsequently verified applicable passing targets and reopens recurring failures.

Execution states are queued → running → completed/failed. A unique database slot prevents overlapping runs. Startup marks interrupted runs failed and releases their slot; **Retry failed scope** creates a new assessment and retains old results. This executor targets one backend process, matching Compose's single-worker configuration.

## Dashboards and YAML

**Overview** and **Reports** use the same assessment snapshot. Select a completed assessment, then filter by restaurant, framework, or severity. Pass rate, state counts, framework summaries, coverage, finding severity, overdue counts, result rows, and **Download YAML** use those exact selections. The assessment stays pinned when a newer scan finishes. **Scan history** opens the execution history; **Manage findings** opens the current remediation workbench.

Finding ownership, due dates, workflow state and verification outcome are frozen in the scan-completion transaction. Overdue counts are calculated as of that completion date (UTC). Assigning an owner or changing a due date later affects future snapshots. Older scans without captured workflow state show historical failure observations and N/A workflow totals. They never borrow current assignments or claim an earlier resolution. Evidence age is measured at assessment evaluation time.

YAML schema **1.0** is defined in `backend/schemas/report-v1.schema.json`. Exports include a report ID/generation time, organization, source assessment and original scope, selected filters, rule versions, coverage, summaries, results, finding snapshots and evidence IDs/hashes/capture times. They exclude auth data, free-text operator notes, internal file paths, source records and raw payment values. Exports use a safe YAML emitter and are validated against typed models; there is no YAML import/editor.

## Scheduled scans

Open **Settings**, choose restaurants/frameworks and an interval from **1 to 10,080 minutes**, enable **Scheduled scans**, and save. The default is disabled with a 60-minute interval. Settings show next run, last attempt, last assessment and collection/execution errors. Configuration persists in SQLite; enabling or changing scope/interval starts a new interval. Disabling prevents new scheduled work and lets an already reserved scan finish.

The one backend worker owns the scheduler through an OS file lock. Both triggers reserve the same unique assessment slot. A due schedule waits while a manual scan is active. Restart marks interrupted assessments failed, retains results, and performs at most one overdue scheduled run rather than replaying every missed interval.

On its first scheduled collection, the app creates ignored editable connector files at `data/mock_sources/connector/`, using the **current imported sources**, including prior corrections. This is separate from committed seeds and immutable evidence/source revisions. The directory has ERP, loyalty and payment JSON plus the five October visit files. Scheduled collection validates the complete source contract, imports changes and pins the new evidence atomically with its assessment reservation. Unchanged inputs create no extra evidence generations. Collection preserves recorded capture times; reading a file again does not make stale evidence fresh.

For a local demonstration, wait for that first collection, disable scheduling while editing a connector file, then enable it again. For example, changing an allowed payment configuration in `payment.json` is collected on the next interval without **Start assessment**. Keep the original data shape and historical visit/transaction/event records. If a correction or CLI import changes current sources while the feed is untouched, the collector synchronizes to those current sources. If both the feed and current imports changed independently, collection stops with an audit event instead of overwriting either. Reconcile the feed with current imports to resume. Do not edit immutable files under `evidence/runtime/` or `data/mock_sources/revisions/`.

## Remediation and mock corrections

Open **Findings**, select a finding, and edit its owner, due date, notes, and working status. Owners are fictional ERP employees; only the General Manager logs in. Supported states are `open`, `in_progress`, `awaiting_verification`, and `resolved`. Resolution cannot be set manually; resolved findings reopen only on a later confirmed target failure. Updates use a revision number to detect stale forms.

Two bounded correction actions have a field-level preview:

| Finding | Action | Mock source change |
|---|---|---|
| R001 / LOY-01 | Restore consent evidence | Append the omitted `CE-C001-01-FIX` proof for `C001-01`, purpose `PUR-MARKETING`, grant dated September 1, 2026, notice `privacy-2026-09` |
| R002 / PAY-01 | Enable TLS | Change `R002-payment.settings_json.tls_enabled` from false to true |

Consent restoration represents recovering previously missing fictional historical evidence, not issuing new consent today. The check still evaluates consent at activity time. No arbitrary source editor or real-system configuration is provided.

**Apply correction** creates an immutable runtime copy under `data/mock_sources/revisions/SRC-…/`: ERP, loyalty, payment JSON, exactly five October visit files, and a SHA-256 manifest. It reconstructs the currently selected source generations, validates the copy through the existing connector, and imports new evidence. Original fixtures and prior snapshots/results remain intact. Changed source capture timestamps advance; consent restoration also recollects unchanged ERP approval data so the refreshed loyalty observation has coherent approval coverage. Financial/visit facts are preserved. Runtime corrections persist across restarts, and repeated actions do not create duplicate events, evidence, or revisions.

Import, finding status, source-version metadata, and audit entries commit in one database transaction. Failed operations roll back accepted rows and remove newly created evidence/runtime files, including partially written files. Corrections refuse to run during an active assessment. Uncommitted orphan directories after a process crash are never selected as accepted sources.

A correction moves the finding to **Awaiting verification**. Choose **Re-scan** to assess its restaurant/framework scope, or start a full assessment. A newer passing target resolves it and links the passing result. Unknown/error/not-applicable results preserve its state; failed verification returns work to In progress. An older queued scan cannot satisfy or clear a newer verification request. Out-of-scope findings remain unchanged. Shared findings require coverage of all known affected restaurants before resolution. Verification uses current source generations; an obsolete pinned pass cannot close a current finding.

The baseline has 47/50 passes (94%) and three findings. Correcting and verifying R001 and R002 produces 49/50 passes (98%), two resolved findings, and the unrelated R003 logging finding still open. Recurring failures reuse the same finding ID and retain assignment, notes, prior evidence, and an audited reopening count.

**Audit** provides read-only, paginated scan/assignment/status/source-change/resolution/reopening history. Filter by finding ID, assessment ID, or event type; open an event to see actor, UTC timestamp, before/after state, and linked findings/assessments. Audit recording starts with Phase 4; existing Phase 3 history is preserved without inventing earlier events.

## API endpoints

| Endpoint | Access | Purpose |
|---|---|---|
| `GET /api/health` | Public | Process liveness |
| `GET /api/ready` | Public | Database/schema readiness; 503 if unavailable |
| `POST /api/auth/login` | Same-origin request | Start administrator session |
| `GET /api/auth/me` | Authenticated | Current identity and CSRF token |
| `POST /api/auth/logout` | Authenticated + CSRF | Revoke current session |
| `GET /api/system` | Authenticated | Workspace metadata |
| `GET /api/overview` | General Manager | Imported counts, assessment coverage, latest assessment, and open findings |
| `GET /api/restaurants` and `/{id}` | General Manager | Restaurant inventory and systems |
| `GET /api/employees` and `/{id}` | General Manager | Employee directory, payroll, and allocations |
| `GET /api/visits` | General Manager | Restaurant/month/member filters and pagination |
| `GET /api/visits/days` | General Manager | One evidence row per restaurant/day, counts, payments, and monthly snapshot reference |
| `GET /api/resource-planning/summary?month=2026-10` | General Manager | Per-restaurant and combined sales/payroll totals |
| `GET /api/data/{dataset}` | General Manager | Allowlisted imported datasets; filters and pagination |
| `GET /api/evidence` and `/{id}` | General Manager | Snapshot metadata and control associations |
| `GET /api/evidence/{id}/content` | General Manager | Hash-verified preview; `?download=true` downloads JSON |
| `GET /api/controls` | General Manager | Ten-control catalog and demo-support framework mappings |
| `GET /api/collections` | General Manager | Source import history |
| `POST /api/assessments` | General Manager + CSRF | Queue a manual assessment with restaurant_ids and framework_ids |
| `GET /api/assessments` and `/{id}` | General Manager | Paginated history and assessment detail/progress |
| `GET /api/assessments/{id}/results` | General Manager | Results filtered by restaurant_id, control_id, status; limit/offset pagination |
| `POST /api/assessments/{id}/retry` | General Manager + CSRF | New assessment for a failed assessment's scope |
| `GET /api/results/{id}` | General Manager | Versioned result, subjects, guidance, and pinned evidence references |
| `GET /api/findings` and `/{id}` | General Manager | Stable findings, last failed result, and failure history |
| `PATCH /api/findings/{id}` | General Manager + CSRF | Owner, due date, notes, working status; optional expected_revision conflict guard |
| `POST /api/findings/{id}/corrections` | General Manager + CSRF | Apply a named, previewed demo correction and await verification |
| `GET /api/audit-events` | General Manager | Read-only audit history; finding/assessment/event-type filters and pagination |

The OpenAPI schema is available at [http://localhost:8080/api/openapi.json](http://localhost:8080/api/openapi.json). Interactive documentation is disabled to keep the runtime independent of documentation CDNs.

## Evidence highlights

Evidence opened from an assessment result, finding, or report highlights failed values or affected records in red. The view uses that assessment's original evidence snapshot, including when a later correction has passed. Hover over a highlighted line for the check and failure reason.

Missing evidence, such as an absent consent grant, has a red explanation identifying the affected customer, purpose, and activity. Unrelated records are not marked as failures. Opening evidence directly uses the latest completed assessment that referenced that snapshot. Original JSON downloads remain unchanged; the existing visit-day navigation remains available.

## Development without containers

Use Python 3.11+ and Node.js 22.12+ (Node 22 LTS recommended). Generate `.env` as above and create a virtual environment at the root:

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements-dev.txt
```

Start the backend in one terminal:

```sh
cd backend
APP_ORIGIN=http://localhost:5173 ../.venv/bin/uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --reload
```

Start the frontend in another:

```sh
cd frontend
npm ci
npm run dev -- --host localhost
```

Open [http://localhost:5173](http://localhost:5173). Vite proxies `/api` to the backend. To seed native development explicitly, run `../.venv/bin/python -m app.seed import` inside `backend/` after startup. Alternatively, set `SEED_DEMO_DATA=true` in `.env` for one-time startup seeding of empty inventory. The same host database/evidence directories are used by native development and Compose; run one backend mode at a time.

Migrations run automatically at API startup. To apply them explicitly, run `../.venv/bin/alembic upgrade head` inside `backend/`. To create a later migration, edit the models then run `../.venv/bin/alembic revision --autogenerate -m "describe change"` and review the generated file before upgrading.

## Verification

Backend tests create temporary databases and never use the configured demo database:

```sh
cd backend
../.venv/bin/python -m pytest -q
```

Frontend type check and production build:

```sh
cd frontend
npm ci
npm run build
```

With the Compose app running, verify the full HTTP flow and session persistence across a backend restart from the repository root:

```sh
.venv/bin/python scripts/smoke_test.py --restart
.venv/bin/python scripts/smoke_test.py --restart --phase2
.venv/bin/python scripts/smoke_test.py --restart --phase3
.venv/bin/python scripts/smoke_phase4.py --restart
.venv/bin/python scripts/smoke_phase5.py
.venv/bin/python scripts/smoke_phase5.py --schedule --restart
```

Smoke tests read local credentials without printing them and verify protected API/session behavior. `--phase3` requires baseline data and adds two 94% assessments. `smoke_phase4.py` is reentrant: it assigns owners, applies both named corrections if needed, verifies selective/full scans and 98% with two resolved/one open, checks all prior evidence/results and financial facts, and optionally restarts to verify persistence. It deliberately updates runtime mock sources and adds history. `--restart` briefly interrupts the backend; omit it to avoid that restart.

`smoke_phase5.py` compares dashboard and safe-parsed YAML for four filter combinations and verifies earlier assessments, evidence hashes, committed seeds, sessions and financial totals. `--schedule` temporarily uses a one-minute schedule, changes only a connector source-version label, waits for automatic collection, then restores the saved settings. `--restart` also checks schedule/session persistence across a backend restart. This adds assessment/audit history and, with `--schedule`, one source metadata generation; it does not reset data or change business facts.
