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
    finally:
        request("/api/auth/logout", {}, csrf=identity["csrf_token"])
    denied("/api/system")
    print("PASS: frontend, readiness, protected API, login, session persistence, logout" if args.restart else "PASS: frontend, readiness, protected API, login, logout")


if __name__ == "__main__":
    main()
