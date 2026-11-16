# MYL Inc. Compliance Monitoring

SJSU CMPE 272 final project: a local compliance monitoring demonstration for MYL Inc.'s restaurant loyalty program (GDPR) and payment systems (PCI DSS).

## Current scope

Phase 1 provides a React/TypeScript/Material UI navigation shell, FastAPI API, administrator login, SQLite persistence, Alembic migrations, and Docker Compose. Restaurant inventory, the ERP connector, compliance evaluations, remediation, scheduled scans, and YAML reports are planned for subsequent phases. The UI displays empty states until those modules exist.

All operational data will be fictional. The planned dataset lists 50 restaurants by ID, with detailed evidence for 5. This is a local demonstration with no external enterprise service dependencies.

## Start locally with Docker

Prerequisites: Docker Desktop (or Docker Engine with Compose), and Python 3 to generate local configuration. Start Docker before running Compose. Initial dependency and container-image downloads need internet access; the running application uses local services and bundled assets only.

From the repository root:

```sh
python3 scripts/configure_local.py
docker compose up --build -d --wait
```

The configuration command creates a private, ignored `.env` with username `admin` and a randomly generated password. It preserves any existing `.env`. Open `.env` locally to view the credentials, then sign in at [http://localhost:8080](http://localhost:8080). No credentials are committed or printed by the configuration script.

After the initial build, start with `docker compose up -d --wait`. Use `docker compose logs` to inspect startup, and `docker compose down` to stop. Database and evidence files remain in the host directories across container removal and recreation.

The app binds to the local loopback interface. Open it using `localhost` as shown above. If you change the port, update both `WEB_PORT` and `APP_ORIGIN` in `.env` to match, then recreate the containers.

## Architecture

```text
Browser -> localhost:8080 -> Nginx / React assets
                              |
                              +-- /api/* -> FastAPI -> SQLite
                                                   -> local evidence directory
```

The API has no published host port in Compose. The frontend reverse proxy keeps browser API requests on the same origin. A single API worker is intentional for the local SQLite application and future serial scan executor.

| Path | Purpose |
|---|---|
| `frontend/` | React, TypeScript, Vite, Material UI, Nginx |
| `backend/app/` | FastAPI application, configuration, authentication, models |
| `backend/migrations/` | Alembic database migrations |
| `backend/tests/` | Isolated backend integration tests |
| `data/myl.db` | Generated SQLite database; ignored by Git |
| `evidence/runtime/` | Generated local evidence; ignored except directory placeholder |
| `.env.example` | Documented configuration template |

Project memory and planning are maintained outside this repository.

## Authentication and configuration

Administrator credentials initialize the database on first startup. Passwords use Argon2 hashes. Sessions use random HttpOnly, SameSite cookies, with only token hashes stored in SQLite. Sessions expire after `SESSION_HOURS` (default 8); logout revokes the server-side session. Mutating browser requests require same-origin headers, and logout also requires the session's CSRF token. Credentials and session cookies are never placed in browser local storage.

For this local demo, passwords must be nonempty (not whitespace-only), must not start with the template marker `replace-`, and must be at most 1024 characters to sign in. There are no uppercase, number, or symbol requirements. The setup script generates a random password for new configurations and preserves existing credentials.

Editing credentials in `.env` does not silently overwrite the existing administrator. To explicitly update credentials and revoke existing sessions:

```sh
# First edit ADMIN_USERNAME / ADMIN_PASSWORD in .env.
docker compose run --rm backend python -m app.bootstrap --reset-password
docker compose up -d --force-recreate --wait
```

`COOKIE_SECURE=false` supports the local HTTP demo. The one-process login throttle allows 10 attempts per minute per client and resets when the API restarts. This setup targets the local demonstration.

## API endpoints

| Endpoint | Access | Purpose |
|---|---|---|
| `GET /api/health` | Public | Process liveness |
| `GET /api/ready` | Public | Database/schema readiness; 503 if unavailable |
| `POST /api/auth/login` | Same-origin request | Start administrator session |
| `GET /api/auth/me` | Authenticated | Current identity and CSRF token |
| `POST /api/auth/logout` | Authenticated + CSRF | Revoke current session |
| `GET /api/system` | Authenticated | Workspace metadata |

The OpenAPI schema is available at [http://localhost:8080/api/openapi.json](http://localhost:8080/api/openapi.json). Interactive documentation is disabled to keep the runtime independent of documentation CDNs.

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

Open [http://localhost:5173](http://localhost:5173). Vite proxies `/api` to the backend. The same host database/evidence directories are used by native development and Compose; run one backend mode at a time.

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
```

The smoke test reads local credentials without printing them, signs in, checks protected API access, optionally restarts the backend, verifies the saved session, and signs out. `--restart` briefly interrupts the local backend. Omit it to test without restarting.
