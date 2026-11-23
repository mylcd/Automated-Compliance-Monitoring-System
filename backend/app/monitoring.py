"""Persistent, single-owner interval scans for the one-process local demo."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import fcntl
import json
import logging
from pathlib import Path
import secrets
from threading import Event, Thread

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from app.assessment_models import Assessment
from app.assessments import AssessmentInput, assessment_dict, stamp, values
from app.audit import add_audit
from app.monitoring_models import ScanSchedule
from app.remediation import source_bundles
from app.seed import canonical, digest, import_sources, load_sources

logger = logging.getLogger(__name__)


def utcnow():
    return datetime.now(timezone.utc)


def fingerprint(bundles):
    stable = deepcopy(bundles)
    for bundle in stable.values():
        for name, rows in bundle["datasets"].items():
            bundle["datasets"][name] = sorted(rows, key=canonical)
    return digest(canonical(stable))


def connector_path(settings):
    root = settings.runtime_source_dir
    path = (root / "connector").resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("Invalid local connector path")
    return path


def atomic_write(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(contents)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def publish_connector(settings, bundles):
    """An editable feed is separate from immutable evidence/revision files."""
    folder = connector_path(settings)
    for source, original in bundles.items():
        bundle = deepcopy(original)
        if source == "loyalty":
            visits = bundle["datasets"].pop("visits")
            for rid in bundle["restaurant_ids"]:
                rows = [row for row in visits if row["restaurant_id"] == rid]
                atomic_write(folder / "visits" / f"{rid}-2026-10.json", canonical(dict(
                    schema_version="1.0", restaurant_id=rid, month="2026-10", currency="USD",
                    captured_at=bundle["captured_at"], records=rows)))
        atomic_write(folder / f"{source}.json", canonical(bundle))
    acknowledge_connector(settings, fingerprint(bundles))


def acknowledge_connector(settings, source_hash):
    atomic_write(connector_path(settings) / ".state.json", canonical({"schema_version": "1.0", "base_fingerprint": source_hash}))


def collect_connector(db, settings, created_files):
    current = source_bundles(db, settings)
    folder = connector_path(settings)
    current_hash = fingerprint(current)
    if not folder.exists():
        publish_connector(settings, current)
    state = json.loads((folder / ".state.json").read_bytes())
    if state.get("schema_version") != "1.0" or not isinstance(state.get("base_fingerprint"), str):
        raise ValueError("Invalid local connector state")
    incoming = load_sources(folder)
    incoming_hash = fingerprint(incoming)
    base = state["base_fingerprint"]
    if current_hash != base and incoming_hash != base and current_hash != incoming_hash:
        raise ValueError("Both the local connector and imported sources changed. Reconcile the connector with the current imported data before the next scan.")
    if current_hash != base and incoming_hash == base:
        # Retain intervening corrections instead of restoring older source data.
        publish_connector(settings, current)
        return current_hash
    if incoming_hash != current_hash:
        import_sources(settings, incoming, db=db, created_files=created_files)
    return incoming_hash


class ScheduleInput(AssessmentInput):
    enabled: bool
    interval_minutes: int = Field(ge=1, le=10080)
    expected_revision: int = Field(ge=0)


class NotDue(Exception):
    pass


class ScanScheduler:
    def __init__(self, settings, factory, service):
        self.settings, self.factory, self.service = settings, factory, service
        self.stop_event, self.wake_event = Event(), Event()
        self.thread, self.owner_file = None, None

    def acquire(self):
        # Enforce ownership before recovery can interrupt another worker's scan.
        database = make_url(self.settings.database_url).database
        if not database or database == ":memory:":
            raise RuntimeError("The monitoring app requires a file-backed SQLite database")
        database_path = Path(database).resolve()
        path = database_path.with_name(f".{database_path.name}.monitor.lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.owner_file = path.open("a")
        try:
            fcntl.flock(self.owner_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.owner_file.close()
            self.owner_file = None
            raise RuntimeError("Run one MYL backend worker per database") from None

    def start(self):
        with self.factory.begin() as db:
            if db.get(ScanSchedule, 1) is None:
                from app.domain import Restaurant
                db.add(ScanSchedule(id=1, enabled=False, interval_minutes=60,
                    restaurant_ids=list(db.scalars(select(Restaurant.id).order_by(Restaurant.id))),
                    framework_ids=["GDPR", "PCI-DSS"], revision=0, updated_at=stamp(), updated_by="system"))
        self.stop_event.clear()
        self.thread = Thread(target=self.loop, name="myl-scan-scheduler", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.wake_event.set()
        if self.thread:
            self.thread.join(timeout=30)
            if self.thread.is_alive():
                logger.error("Scheduled assessment is still stopping; recovery will fence unfinished work")
        if self.owner_file:
            fcntl.flock(self.owner_file, fcntl.LOCK_UN)
            self.owner_file.close()
            self.owner_file = None

    def loop(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("Scheduler tick failed; next tick will retry")
            self.wake_event.wait(self.settings.scheduler_poll_seconds)
            self.wake_event.clear()

    def tick(self, at=None):
        at = at or utcnow()
        with self.factory() as db:
            schedule = db.get(ScanSchedule, 1)
            if not schedule or not schedule.enabled or not schedule.next_run_at or datetime.fromisoformat(schedule.next_run_at.replace("Z", "+00:00")) > at:
                return None
            revision = schedule.revision
            payload = AssessmentInput(restaurant_ids=schedule.restaurant_ids, framework_ids=schedule.framework_ids)
        created_files, source_hash = [], None

        def collect(db):
            nonlocal source_hash
            schedule = db.get(ScanSchedule, 1)
            if not schedule.enabled or schedule.revision != revision or not schedule.next_run_at or datetime.fromisoformat(schedule.next_run_at.replace("Z", "+00:00")) > at:
                raise NotDue()
            source_hash = collect_connector(db, self.settings, created_files)

        def queued(db, assessment):
            schedule = db.get(ScanSchedule, 1)
            schedule.last_run_at, schedule.last_assessment_id, schedule.last_error = stamp(at), assessment.id, None
            schedule.next_run_at = stamp(at + timedelta(minutes=schedule.interval_minutes))
            add_audit(db, "schedule_triggered", "scheduler", assessment_id=assessment.id,
                      details={"schedule_revision": revision, "next_run_at": schedule.next_run_at}, occurred_at=stamp(at))

        try:
            accepted = self.service.queue(payload, "scheduler", trigger="scheduled", before_snapshot=collect, on_queued=queued)
        except Exception as error:
            for path in created_files:
                path.unlink(missing_ok=True)
            if isinstance(error, NotDue):
                return None
            with self.factory.begin() as db:
                if isinstance(error, HTTPException) and db.scalar(select(Assessment.id).where(Assessment.active_slot == 1)):
                    return None  # Retry the unchanged due time after the shared slot clears.
                schedule = db.get(ScanSchedule, 1)
                if schedule.revision == revision and schedule.enabled:
                    schedule.last_run_at = stamp(at)
                    schedule.last_error = "Local connector collection failed. Validate its JSON files and reconcile pending imports before retrying."
                    schedule.next_run_at = stamp(at + timedelta(minutes=schedule.interval_minutes))
                    add_audit(db, "schedule_collection_failed", "scheduler", after={"error": schedule.last_error}, occurred_at=stamp(at))
            logger.warning("Scheduled collection failed (%s)", type(error).__name__)
            return None
        try:
            acknowledge_connector(self.settings, source_hash)
        except OSError:
            logger.warning("Connector acknowledgement will be retried on the next tick")
        self.service.run(accepted["id"])
        return accepted["id"]


def create_monitoring_router(database, current_session, browser_write, scheduler):
    router = APIRouter(prefix="/api", dependencies=[Depends(current_session)])

    def serialize(db, schedule):
        last = db.get(Assessment, schedule.last_assessment_id) if schedule.last_assessment_id else None
        active = db.scalar(select(Assessment).where(Assessment.active_slot == 1))
        return {**values(schedule), "last_assessment": assessment_dict(last) if last else None,
                "active_assessment": assessment_dict(active) if active else None,
                "collector": "Local mock connector", "poll_seconds": scheduler.settings.scheduler_poll_seconds}

    @router.get("/schedule")
    def get_schedule(db=Depends(database)):
        return serialize(db, db.get(ScanSchedule, 1))

    @router.put("/schedule", dependencies=[Depends(browser_write)])
    def save_schedule(payload: ScheduleInput, request: Request, identity=Depends(current_session), db=Depends(database)):
        if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), identity[1].csrf_token):
            raise HTTPException(403, "Invalid CSRF token")
        db.execute(text("BEGIN IMMEDIATE"))
        from app.domain import Restaurant
        if set(db.scalars(select(Restaurant.id).where(Restaurant.id.in_(payload.restaurant_ids)))) != set(payload.restaurant_ids):
            raise HTTPException(422, "Select existing restaurants")
        schedule = db.get(ScanSchedule, 1)
        if schedule.revision != payload.expected_revision:
            raise HTTPException(409, "Schedule changed. Refresh it before saving.")
        before = values(schedule)
        changed = any(getattr(schedule, key) != getattr(payload, key) for key in ("enabled", "interval_minutes", "restaurant_ids", "framework_ids"))
        if changed:
            for key in ("enabled", "interval_minutes", "restaurant_ids", "framework_ids"):
                setattr(schedule, key, getattr(payload, key))
            schedule.updated_at, schedule.updated_by = stamp(), identity[0].username
            schedule.revision += 1
            schedule.next_run_at = stamp(utcnow() + timedelta(minutes=schedule.interval_minutes)) if schedule.enabled else None
            schedule.last_error = None
            add_audit(db, "schedule_updated", identity[0].username, before=before, after=values(schedule), occurred_at=schedule.updated_at)
        db.commit()
        scheduler.wake_event.set()
        return serialize(db, schedule)

    return router
