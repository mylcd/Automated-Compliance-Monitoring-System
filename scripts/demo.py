"""Operate only the isolated presentation copy at http://127.0.0.1:8081.

Uses separate Compose project, database, evidence, sources, hostname and cookies.
The primary localhost:8080 application's runtime directories are never mounted.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT.parent / "phase6_demo"
ORIGIN = "http://127.0.0.1:8081"
PROJECT = "myl-rehearsal"


def scoped_directory():
    expected = ROOT.parent.resolve() / "phase6_demo"
    if DEMO.resolve() != expected or DEMO.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError("Rehearsal directory must be the separate phase6_demo folder")
    for name in ("data", "evidence"):
        candidate = (DEMO / name).resolve()
        if not candidate.is_relative_to(expected):
            raise ValueError("Rehearsal runtime directories must not be symlinks outside the demo")
    return expected


def configuration():
    folder = scoped_directory()
    return {"name": PROJECT, "services": {
        "backend": {
            "image": "myl-rehearsal-backend", "build": {"context": str(ROOT / "backend")},
            "environment": {
                "ADMIN_USERNAME": "${ADMIN_USERNAME:-admin}",
                "ADMIN_PASSWORD": "${ADMIN_PASSWORD:?Configure the primary private .env first}",
                "APP_ORIGIN": ORIGIN, "SESSION_HOURS": "8", "COOKIE_SECURE": "false",
                "SEED_DEMO_DATA": "true", "ASSESSMENT_MAX_EVIDENCE_AGE_DAYS": "30",
                "DATABASE_URL": "sqlite:////app/data/myl.db", "EVIDENCE_DIR": "/app/evidence"},
            "volumes": [str(folder / "data") + ":/app/data", str(folder / "evidence") + ":/app/evidence"],
            "healthcheck": {"test": ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/ready', timeout=3)"],
                            "interval": "3s", "timeout": "5s", "retries": 20, "start_period": "10s"},
            "restart": "unless-stopped"},
        "frontend": {
            "image": "myl-rehearsal-frontend", "build": {"context": str(ROOT / "frontend")},
            "ports": ["127.0.0.1:8081:80"], "depends_on": {"backend": {"condition": "service_healthy"}},
            "restart": "unless-stopped"}}}


def prepare():
    folder = scoped_directory()
    if not (ROOT / ".env").is_file():
        raise ValueError("Run python3 scripts/configure_local.py first")
    folder.mkdir(exist_ok=True)
    for name in ("data", "evidence"):
        (folder / name).mkdir(exist_ok=True)
    manifest = folder / "compose.json"
    manifest.write_text(json.dumps(configuration(), indent=2) + "\n")
    return manifest


def compose(*arguments, capture=False):
    manifest = prepare()
    return subprocess.run(["docker", "compose", "--project-name", PROJECT,
        "--env-file", str(ROOT / ".env"), "--file", str(manifest), *arguments],
        cwd=ROOT, check=True, text=True, capture_output=capture)


def backend_python(code, capture=False):
    return compose("exec", "-T", "backend", "python", "-c", code, capture=capture)


def reset_demo():
    # This command is deliberately limited to the isolated backend. The seed
    # reset itself also rejects an active assessment and preserves credentials.
    backend_python('''
import time
from sqlalchemy import select
from app.config import Settings
from app.database import create_database_engine, session_factory
from app.assessment_models import Assessment
from app.monitoring_models import ScanSchedule
from app.seed import reset_demo, import_sources, load_sources
settings = Settings()
engine = create_database_engine(settings.database_url)
factory = session_factory(engine)
with factory.begin() as db:
    schedule = db.get(ScanSchedule, 1)
    if schedule:
        schedule.enabled = False
        schedule.interval_minutes = 60
        schedule.next_run_at = None
        schedule.revision += 1
deadline = time.monotonic() + 30
while True:
    with factory() as db:
        active = db.scalar(select(Assessment.id).where(Assessment.active_slot == 1))
    if not active:
        break
    if time.monotonic() >= deadline:
        raise RuntimeError("Wait for the active rehearsal assessment and retry")
    time.sleep(.1)
reset_demo(settings)
import_sources(settings, load_sources(scenario="unauthorized-modification"))
with factory.begin() as db:
    schedule = db.get(ScanSchedule, 1)
    if schedule:
        schedule.restaurant_ids = [f"R{i:03}" for i in range(1, 6)]
        schedule.framework_ids = ["GDPR", "PCI-DSS"]
engine.dispose()
print("Isolated demo reset: five restaurants, four-failure presentation sources including LOY-05, no assessments; schedule disabled.")
''')


def add_loy05():
    """Append the existing unauthorized-action fixture without resetting history."""
    backend_python('''
from datetime import datetime, timezone
from sqlalchemy import select
from app.config import Settings
from app.database import create_database_engine, session_factory
from app.assessment_models import Assessment
from app.monitoring import connector_path, fingerprint, publish_connector
from app.remediation import source_bundles
from app.seed import import_sources, load_sources
from app.evaluators import instant
settings = Settings()
engine = create_database_engine(settings.database_url)
try:
    with session_factory(engine)() as db:
        if db.scalar(select(Assessment.id).where(Assessment.active_slot == 1)):
            raise ValueError("Wait for the active presentation assessment before adding the case")
        bundles = source_bundles(db, settings)
    event = next(row for row in load_sources(scenario="unauthorized-modification")["loyalty"]["datasets"]["employee_access_events"] if row["id"] == "EV-UNAUTHORIZED-001")
    rows = bundles["loyalty"]["datasets"]["employee_access_events"]
    existing = next((row for row in rows if row["id"] == event["id"]), None)
    if existing and existing != event:
        raise ValueError("The presentation event ID already contains different historical data")
    refresh_approvals = instant(bundles["erp"]["captured_at"]) < instant(bundles["loyalty"]["captured_at"])
    if existing is None or refresh_approvals:
        folder = connector_path(settings)
        if folder.exists() and fingerprint(load_sources(folder)) != fingerprint(bundles):
            raise ValueError("Collect or reconcile pending connector edits before adding the case")
        if existing is None:
            rows.append(event)
        now = datetime.now(timezone.utc)
        # Recollect approvals and observed permissions at the same instant.
        # Older approval evidence cannot substantiate newer observed grants.
        for bundle in bundles.values():
            bundle["captured_at"] = now.isoformat().replace("+00:00", "Z")
            bundle["source_version"] = "myl-rehearsal-loy05-" + now.strftime("%Y%m%dT%H%M%S%fZ")
        import_sources(settings, bundles)
        publish_connector(settings, bundles)
        print("Added R001 LOY-05 action evidence. Run a GDPR assessment to detect the finding.")
    else:
        print("R001 LOY-05 action evidence is already present; no duplicate was added.")
finally:
    engine.dispose()
''')


def prepare_feed():
    backend_python('''
from app.config import Settings
from app.database import create_database_engine, session_factory
from app.monitoring import connector_path, publish_connector
from app.remediation import source_bundles
settings = Settings()
if not connector_path(settings).exists():
    engine = create_database_engine(settings.database_url)
    with session_factory(engine)() as db:
        publish_connector(settings, source_bundles(db, settings))
    engine.dispose()
print("Isolated editable connector is ready.")
''')


def set_tls(enabled):
    prepare_feed()
    path = scoped_directory() / "data" / "mock_sources" / "connector" / "payment.json"
    if not path.resolve().is_relative_to(scoped_directory()):
        raise ValueError("Invalid isolated source path")
    payload = json.loads(path.read_bytes())
    row = next(row for row in payload["datasets"]["payment_configurations"] if row["asset_id"] == "R002-payment")
    if not isinstance(row["settings_json"].get("tls_enabled"), bool):
        raise ValueError("Expected a boolean TLS setting")
    row["settings_json"]["tls_enabled"] = enabled
    now = datetime.now(timezone.utc)
    payload["captured_at"] = now.isoformat().replace("+00:00", "Z")
    payload["source_version"] = "myl-rehearsal-" + now.strftime("%Y%m%dT%H%M%S%fZ")
    pending = path.with_suffix(".pending")
    pending.write_text(json.dumps(payload, indent=2) + "\n")
    pending.replace(path)
    print(f"Isolated R002 TLS = {str(enabled).lower()}. The next scheduled collection will evaluate it.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["start", "status", "stop", "reset", "add-loy05", "prepare-feed", "tls-off", "tls-on"])
    parser.add_argument("--yes", action="store_true", help="Confirm reset of the isolated rehearsal copy only")
    parser.add_argument("--build", action="store_true", help="Rebuild isolated images on start")
    args = parser.parse_args()
    if args.command == "start":
        build = args.build or any(subprocess.run(["docker", "image", "inspect", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode for name in ("myl-rehearsal-backend", "myl-rehearsal-frontend"))
        compose("up", "-d", "--wait", *(["--build"] if build else []))
        add_loy05()
        print(f"Presentation copy: {ORIGIN} (same administrator credentials as your private .env).")
    elif args.command == "status":
        compose("ps")
        print(ORIGIN)
    elif args.command == "stop":
        compose("down")
    elif args.command == "reset":
        if not args.yes:
            parser.error("reset requires --yes; only the isolated phase6_demo history/data is cleared")
        reset_demo()
    elif args.command == "prepare-feed":
        prepare_feed()
    elif args.command == "add-loy05":
        add_loy05()
    else:
        set_tls(args.command == "tls-on")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"Demo operation failed: {type(error).__name__}. Check Docker and the isolated demo configuration.", file=sys.stderr)
        raise SystemExit(1)
