from __future__ import annotations

import json
import shutil
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, sync_playwright


ROOT = Path(__file__).resolve().parents[2]
AXE_SOURCE = (Path(__file__).parent / "assets" / "axe.min.js").read_text(encoding="utf-8")

class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):  # pragma: no cover - test server noise
        pass


@pytest.fixture(scope="session")
def web_server():
    handler = lambda *args, **kwargs: QuietHandler(*args, directory=str(ROOT / "web"), **kwargs)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    thread.join(timeout=2)


@pytest.fixture
def page(web_server):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=shutil.which("google-chrome-stable") or shutil.which("google-chrome")
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            extra_http_headers={"X-Forwarded-User": "operator@example.test"},
        )
        page = context.new_page()
        status = {
            "generation": 1,
            "observed_at": "2026-07-16T12:00:00Z",
            "profiles": [
                {
                    "profile_id": "minecraft",
                    "state": "running",
                    "health": "healthy",
                    "slot_owner": "minecraft",
                    "active_job_id": None,
                    "pid": 101,
                    "started_at": "2026-07-16T10:00:00Z",
                    "uptime_seconds": 7200,
                    "cpu_percent": 7.4,
                    "rss_bytes": 128000000,
                    "players_online": 4,
                    "installed_version": "1.21.8",
                    "restart_required": False,
                    "required_ports_ready": True,
                },
                {
                    "profile_id": "terraria-vanilla",
                    "state": "stopped",
                    "health": "unknown",
                    "slot_owner": None,
                    "active_job_id": None,
                    "pid": None,
                    "started_at": None,
                    "uptime_seconds": None,
                    "cpu_percent": None,
                    "rss_bytes": None,
                    "players_online": 0,
                    "installed_version": "1.4.4.9",
                    "restart_required": False,
                    "required_ports_ready": False,
                },
                {
                    "profile_id": "terraria-tmod",
                    "state": "failed",
                    "health": "unhealthy",
                    "slot_owner": None,
                    "active_job_id": None,
                    "pid": None,
                    "started_at": None,
                    "uptime_seconds": None,
                    "cpu_percent": None,
                    "rss_bytes": None,
                    "players_online": None,
                    "installed_version": "2024.12",
                    "restart_required": True,
                    "required_ports_ready": False,
                },
            ],
        }
        names = [
            {"id": "minecraft", "display_name": "Minecraft", "adapter": "crafty", "operations": ["start", "stop", "restart"]},
            {"id": "terraria-vanilla", "display_name": "Terraria Vanilla", "adapter": "systemd", "operations": ["start", "stop", "restart", "command"]},
            {"id": "terraria-tmod", "display_name": "Terraria tModLoader", "adapter": "systemd", "operations": ["start", "stop", "restart", "command"]},
        ]
        schedules = [
            {"cron": "0 20 * * 5", "profile": "minecraft", "next_fire": "2026-07-17T20:00:00Z", "enabled": True},
            {"cron": "30 6 * * 1", "profile": "terraria-tmod", "next_fire": None, "enabled": False},
        ]

        def fulfill(route):
            request = route.request
            path = urlparse(request.url).path
            if path == "/api/v1/session":
                return route.fulfill(json={"actor": "operator@example.test", "csrf_token": "test-token", "expires_at": None})
            if path == "/api/v1/status":
                return route.fulfill(json=status)
            if path == "/api/v1/profiles":
                return route.fulfill(json=names)
            if path == "/api/v1/perf":
                return route.fulfill(json={})
            if path == "/api/v1/schedules":
                return route.fulfill(json={"schedules": schedules})
            if path == "/api/v1/stream":
                payload = json.dumps(status, separators=(",", ":"))
                return route.fulfill(
                    status=200,
                    headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
                    body=f"event: status\ndata: {payload}\n\n",
                )
            if path.endswith("/logs"):
                return route.fulfill(json={"items": [{
                    "timestamp": "2026-07-16T12:00:00Z",
                    "severity": "info",
                    "message": "server ready",
                }], "next_cursor": None})
            if path == "/api/v1/backups":
                return route.fulfill(json={"items": [{
                    "id": "minecraft-backup", "profile_id": "minecraft",
                    "created_at": "2026-07-15T12:00:00Z", "size_bytes": 1073741824,
                    "verified": True, "protected": False,
                }], "next_cursor": None})
            if path.endswith("/backups"):
                profile_id = path.split("/")[4]
                return route.fulfill(json={"items": [{
                    "id": f"{profile_id}-backup",
                    "profile_id": profile_id,
                    "created_at": "2026-07-15T12:00:00Z",
                    "size_bytes": 1073741824,
                    "verified": True,
                    "protected": False,
                }], "next_cursor": None})
            if path.endswith("/config"):
                return route.fulfill(json={"profile_id": "minecraft", "settings": [
                    {"key": "motd", "value": "Hello", "type": "str", "bounds": {"max_length": 59}, "restart_required": True},
                    {"key": "max-players", "value": 8, "type": "int", "bounds": {"min": 1, "max": 64}, "restart_required": True},
                    {"key": "pvp", "value": True, "type": "bool", "bounds": {}, "restart_required": True},
                ]})
            if path.endswith("/notifications"):
                return route.fulfill(json={"rules": {}})
            if "/stats/" in path:
                return route.fulfill(json={"window": "24h", "samples": [], "buckets": []})
            return route.fulfill(json={"ok": True})

        page.route("**/api/v1/**", fulfill)
        console_errors = []
        page_errors = []
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.goto(f"{web_server}#/")
        page.wait_for_selector('[data-profile-id="minecraft"]', timeout=5000)
        page.add_script_tag(content=AXE_SOURCE)
        yield page
        assert console_errors == []
        assert page_errors == []
        context.close()
        browser.close()


def scan_surface(page: Page, surface: str) -> None:
    violations = page.evaluate(
        """async () => {
            const results = await axe.run(document);
            return results.violations
                .filter((violation) => ["serious", "critical"].includes(violation.impact))
                .map((violation) => ({
                    id: violation.id,
                    impact: violation.impact,
                    nodes: violation.nodes.map((node) => node.target),
                }));
        }"""
    )
    remaining = []
    for violation in violations:
        nodes = violation["nodes"]
        if nodes:
            remaining.append({**violation, "nodes": nodes})
    if not remaining:
        return
    details = []
    for violation in remaining:
        selectors = [" ".join(target) if isinstance(target, list) else str(target) for target in violation["nodes"]]
        details.append(f"{violation['id']} ({violation['impact']}): {', '.join(selectors)}")
    pytest.fail(f"axe violations on {surface}:\n" + "\n".join(details))


def test_a11y_guard_scans_shipped_console_surfaces(page: Page):
    assert page.locator("#profile-cards > [data-profile-family]").count() == 3
    scan_surface(page, "dashboard (family groups)")

    base = page.url.split("#", 1)[0]
    page.goto(f"{base}#/servers/minecraft/console")
    page.wait_for_selector("#panel-console:not([hidden])")
    scan_surface(page, "profile detail (Console tab)")

    page.goto(f"{base}#/servers/minecraft/config")
    page.wait_for_selector("#panel-config:not([hidden])")
    page.wait_for_selector("#schedule-list [data-schedule-row]")
    assert page.locator("#schedule-list .schedule-row.is-disabled").count() >= 1
    scan_surface(page, "Config tab with schedule rows")

    page.goto(f"{base}#/backups")
    page.wait_for_selector("#backups-view:not([hidden])")
    page.wait_for_selector("#aggregate-backup-list li:not(.empty-state)")
    scan_surface(page, "Backups view")

    page.goto(f"{base}#/")
    page.wait_for_selector('[data-profile-id="minecraft"]')
    page.get_by_role("button", name="Command palette").click()
    page.wait_for_selector("#command-palette[open]")
    scan_surface(page, "command palette open")
