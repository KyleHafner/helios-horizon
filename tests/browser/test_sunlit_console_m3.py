from __future__ import annotations

import shutil
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, sync_playwright


ROOT = Path(__file__).resolve().parents[2]


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


@pytest.fixture
def sunlit_page():
    handler = lambda *args, **kwargs: _QuietHandler(*args, directory=str(ROOT / "web"), **kwargs)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=shutil.which("google-chrome-stable") or shutil.which("google-chrome")
        )
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        status = {
            "generation": 1,
            "observed_at": "2026-08-06T12:00:00Z",
            "profiles": [
                {"profile_id": "minecraft-sunlit-cobblemon", "state": "running", "health": "healthy", "slot_owner": "minecraft-sunlit-cobblemon", "players_online": 1, "required_ports_ready": True},
                {"profile_id": "terraria-vanilla", "state": "stopped", "health": "unknown", "slot_owner": None, "players_online": None, "required_ports_ready": False},
                {"profile_id": "terraria-tmod", "state": "stopped", "health": "unknown", "slot_owner": None, "players_online": None, "required_ports_ready": False},
            ],
        }
        profiles = [
            {"id": "minecraft-sunlit-cobblemon", "display_name": "Sunlit Cobblemon", "adapter": "systemd", "operations": ["start", "stop", "restart", "command", "backup"]},
            {"id": "terraria-vanilla", "display_name": "Terraria Vanilla", "adapter": "systemd", "operations": ["start", "stop", "restart", "command"]},
            {"id": "terraria-tmod", "display_name": "Terraria tModLoader", "adapter": "systemd", "operations": ["start", "stop", "restart", "command"]},
        ]

        def fulfill(route):
            request = route.request
            path = urlparse(request.url).path
            if path == "/api/v1/session":
                return route.fulfill(json={"actor": "operator", "csrf_token": "csrf", "expires_at": None})
            if path == "/api/v1/profiles":
                return route.fulfill(json=profiles)
            if path == "/api/v1/status":
                return route.fulfill(json=status)
            if path == "/api/v1/stream":
                return route.fulfill(status=200, headers={"Content-Type": "text/event-stream"}, body="event: status\ndata: {}\n\n")
            if path == "/api/v1/schedules":
                return route.fulfill(json={"schedules": []})
            if path == "/api/v1/perf":
                return route.fulfill(json={})
            if path.endswith("/logs"):
                return route.fulfill(json={"items": [{"timestamp": "2026-08-06T12:00:00Z", "severity": "info", "message": "server ready"}], "next_cursor": None})
            if path.endswith("/stats/summary"):
                return route.fulfill(json={"total_hours": 0, "unique_players": 0, "leaderboard": [], "player_tracking": "names", "occupancy": {"latest": 1, "samples": []}})
            if path.endswith("/stats/heatmap"):
                return route.fulfill(json={"buckets": [[0 for _ in range(24)] for _ in range(7)]})
            if path.endswith("/stats/tps"):
                return route.fulfill(json={"window": "24h", "samples": [{"ts": "2026-08-06T11:59:30Z", "tps": 20, "mspt": 12}], "stale": False, "state": "ok"})
            if path.endswith("/backups"):
                return route.fulfill(json={"items": [], "next_cursor": None})
            if path.endswith("/config"):
                return route.fulfill(json={"settings": []})
            return route.fulfill(json={"ok": True})

        page.route("**/api/v1/**", fulfill)
        page.goto(f"http://127.0.0.1:{server.server_port}/#/servers/minecraft-sunlit-cobblemon/console")
        page.wait_for_selector("#detail-view:not([hidden])")
        yield page
        context.close()
        browser.close()
    server.shutdown()
    thread.join(timeout=2)


def test_sunlit_console_input_output_and_tps_state_are_truthful(sunlit_page: Page):
    page = sunlit_page
    command = "say private-message-that-must-not-echo"
    assert page.locator("#command-input").is_enabled()
    assert "available" in page.locator("#command-note").inner_text().lower()
    with page.expect_request(lambda request: request.method == "POST" and request.url.endswith("/minecraft-sunlit-cobblemon/command")):
        page.locator("#command-input").fill(command)
        page.locator("#command-send").click()
    page.wait_for_function("document.querySelector('#command-input').value === ''")
    assert page.locator("#command-input").input_value() == ""
    assert command not in page.locator("#console-output").inner_text()

    page.goto(f"{page.url.split('#')[0]}#/servers/minecraft-sunlit-cobblemon/stats")
    page.wait_for_selector("#detail-view:not([hidden])")
    page.wait_for_selector("#stats-tps-current")
    assert page.locator("#stats-tps-current").inner_text() == "20.00 TPS"
