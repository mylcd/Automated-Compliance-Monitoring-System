"""Regressions for old verification requests and interrupted evidence writes."""
from pathlib import Path

from test_phase4 import baseline, client, database_snapshot, detail, factory, files, phase3_seeded, seeded, settings
from test_phase3 import write_headers


def test_older_queued_failure_does_not_clear_new_verification_request(client, baseline, monkeypatch):
    finding = baseline[2][("PAY-01", "R002")]
    deferred = []
    with monkeypatch.context() as patcher:
        patcher.setattr(client.app.state, "assessment_runner", deferred.append)
        queued = client.post("/api/assessments", json={"restaurant_ids": ["R002"], "framework_ids": ["PCI-DSS"]}, headers=write_headers(client))
    assert queued.status_code == 202 and deferred == [queued.json()["id"]]
    waiting = client.patch(f"/api/findings/{finding['id']}", json={"status": "awaiting_verification", "expected_revision": finding["revision"]}, headers=write_headers(client))
    assert waiting.status_code == 200
    client.app.state.assessment_service.run(deferred[0])
    assert detail(client, finding)["status"] == "awaiting_verification"
    events = client.get("/api/audit-events", params={"finding_id": finding["id"], "event_type": "verification_failed"}).json()
    assert events["total"] == 0


def test_partial_evidence_write_is_removed_on_correction_rollback(client, baseline, seeded, factory, monkeypatch):
    before_database = database_snapshot(factory)
    before_evidence = files(seeded.evidence_dir)
    before_sources = files(seeded.runtime_source_dir)
    real_open = Path.open
    failed = []

    class PartialWrite:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def write(self, content):
            self.stream.write(content[:12])
            self.stream.flush()
            failed.append(True)
            raise OSError("Simulated disk write failure")

        def __exit__(self, *_):
            self.stream.close()

    def faulty_open(path, mode="r", *args, **kwargs):
        stream = real_open(path, mode, *args, **kwargs)
        if mode == "xb" and path.is_relative_to(seeded.evidence_dir):
            return PartialWrite(stream)
        return stream

    monkeypatch.setattr(Path, "open", faulty_open)
    finding = baseline[2][("LOY-01", "R001")]
    response = client.post(f"/api/findings/{finding['id']}/corrections", json={"action": "restore_consent_evidence", "expected_revision": finding["revision"]}, headers=write_headers(client))
    assert response.status_code == 409 and failed
    assert database_snapshot(factory) == before_database
    assert files(seeded.evidence_dir) == before_evidence
    assert files(seeded.runtime_source_dir) == before_sources
