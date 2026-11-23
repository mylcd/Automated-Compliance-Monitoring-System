"""Exercise the running local app; never print credentials, cookies, or CSRF tokens."""
import argparse
import http.cookiejar
import json
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request

from dotenv import dotenv_values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--restart", action="store_true")
    parser.add_argument("--phase2", action="store_true", help="Check seeded inventory, evidence and resource totals")
    parser.add_argument("--phase3", action="store_true", help="Run two baseline assessments and verify findings/history")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config = dotenv_values(root / ".env")
    origin = config.get("APP_ORIGIN") or "http://localhost:8080"
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(path, payload=None, csrf=None):
        headers = {"X-Requested-With": "XMLHttpRequest", "Origin": origin}
        if csrf:
            headers["X-CSRF-Token"] = csrf
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        with opener.open(urllib.request.Request(origin + path, data=data, headers=headers), timeout=5) as response:
            body = response.read()
            return json.loads(body) if body else None

    def denied(path):
        try:
            request(path)
        except urllib.error.HTTPError as error:
            assert error.code == 401, f"Unexpected access status: {error.code}"
        else:
            raise AssertionError("Unauthenticated access was permitted")

    with opener.open(origin, timeout=5) as response:
        assert b'<div id="root">' in response.read(), "Frontend HTML missing"
    assert request("/api/ready")["status"] == "ready"
    denied("/api/system")
    identity = request("/api/auth/login", {"username": config["ADMIN_USERNAME"], "password": config["ADMIN_PASSWORD"]})
    try:
        assert request("/api/system")["organization"] == "MYL Inc."
        if args.phase2 or args.phase3:
            overview = request("/api/overview")
            assert (overview["restaurants"], overview["employees"], overview["visits"], overview["controls"]) == (5, 61, 3375, 10)
            assert 0 <= overview["assessed_restaurants"] <= 5
            assert request("/api/employees/E-GM")["platform_access"] is True
            assert request("/api/visits?restaurant_id=R001&month=2026-10&member=false")["total"] > 0
            assert request("/api/visits?month=2026-11")["total"] == 0
            days = request("/api/visits/days?month=2026-10&limit=200")
            assert days["total"] == 155 and days["visits_total"] == 3375
            current_visit_evidence = {day["evidence_id"] for day in days["items"]}
            assert len(current_visit_evidence) == 5
            assert request("/api/evidence?dataset=visits&month=2026-10")["total"] >= 5
            summary = request("/api/resource-planning/summary?month=2026-10")
            assert summary["complete"] is True
            total = summary["totals"][0]
            assert summary["tax_rate_percent"] == 10
            assert total["total_payments_minor"] == 68588172
            assert total["total_payments_minor"] == sum(day["payments"]["USD"] for day in days["items"])
            assert total["total_sales_minor"] + total["total_tax_minor"] == total["total_payments_minor"]
            assert total["total_sales_minor"] < total["total_payments_minor"]
            for key in ["total_sales_minor", "total_payroll_minor", "total_payments_minor", "total_tax_minor"]:
                assert summary["totals"][0][key] == sum(row["totals"][0][key] for row in summary["restaurants"])
            item = request("/api/evidence?dataset=employee_payroll")["items"][0]
            assert len(request(f"/api/evidence/{item['id']}/content")["records"]) == 61
        if args.phase3:
            payload = {"restaurant_ids": [f"R{i:03}" for i in range(1, 6)], "framework_ids": ["GDPR", "PCI-DSS"]}

            def scan():
                accepted = request("/api/assessments", payload, csrf=identity["csrf_token"])
                deadline = time.monotonic() + 30
                while True:
                    result = request(f"/api/assessments/{accepted['id']}")
                    if result["status"] not in ["queued", "running"]:
                        break
                    assert time.monotonic() < deadline, "Assessment did not finish"
                    time.sleep(0.2)
                assert result["status"] == "completed", "Assessment failed"
                assert result["completed_evaluations"] == result["total_evaluations"] == 50
                assert result["summary"]["counts"] == {"pass": 47, "fail": 3, "unknown": 0, "error": 0, "not_applicable": 0}
                assert result["summary"]["pass_rate"] == 94
                assert result["summary"]["coverage"] == {"total_restaurants": 5, "in_scope_restaurants": 5, "assessed_restaurants": 5}
                assert {(r["restaurant_id"], r["control_id"]) for r in result["results"] if r["status"] == "fail"} == {("R001", "LOY-01"), ("R002", "PAY-01"), ("R003", "PAY-03")}
                return result

            first = scan()
            first_findings = request("/api/findings")["items"]
            assert len(first_findings) == 3
            second = scan()
            second_findings = request("/api/findings")["items"]
            assert {f["id"] for f in first_findings} == {f["id"] for f in second_findings}
            assert first["id"] != second["id"]
            assert request(f"/api/assessments/{first['id']}")["results"] == first["results"]
            detail = request(f"/api/findings/{second_findings[0]['id']}")
            assert detail["history"] and detail["result"]["evidence"]
            assert request(f"/api/evidence/{detail['result']['evidence'][0]['id']}/content")["schema_version"] == "1.0"
            assert request("/api/overview")["assessed_restaurants"] == 5
        if args.restart:
            subprocess.run(["docker", "compose", "restart", "backend"], cwd=root, check=True)
            deadline = time.monotonic() + 50
            while True:
                try:
                    if request("/api/ready")["status"] == "ready":
                        break
                except (OSError, urllib.error.URLError):
                    pass
                if time.monotonic() >= deadline:
                    raise AssertionError("Backend did not recover after restart")
                time.sleep(0.5)
        assert request("/api/auth/me")["username"] == config["ADMIN_USERNAME"]
        if args.phase2 or args.phase3:
            assert request("/api/resource-planning/summary?month=2026-10")["totals"] == summary["totals"]
        if args.phase3:
            assert request(f"/api/assessments/{second['id']}")["summary"] == second["summary"]
            assert request("/api/findings")["total"] == 3
    finally:
        request("/api/auth/logout", {}, csrf=identity["csrf_token"])
    denied("/api/system")
    print("PASS: frontend, readiness, protected API, login, session persistence, logout" if args.restart else "PASS: frontend, readiness, protected API, login, logout")
    if args.phase2 or args.phase3:
        print("PASS: 5 October visit seeds, 155 daily groups, 3375 visits, tripled receipts, 10% inclusive tax, reconciled net sales/payroll")
    if args.phase3:
        print("PASS: two 50-result assessments, 94% automated-check pass rate, stable findings, pinned evidence and persisted history")


if __name__ == "__main__":
    main()
