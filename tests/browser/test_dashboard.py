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
        browser = playwright.chromium.launch(executable_path=shutil.which("google-chrome-stable") or shutil.which("google-chrome"))
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            extra_http_headers={"X-Forwarded-User": "operator@example.test"},
        )
        page = context.new_page()
        status = {
            "generation": 1,
            "observed_at": "2026-07-11T12:00:00Z",
            "profiles": [
                {
                    "profile_id": "minecraft",
                    "state": "running",
                    "health": "healthy",
                    "slot_owner": "minecraft",
                    "active_job_id": None,
                    "pid": 101,
                    "started_at": "2026-07-11T10:00:00Z",
                    "uptime_seconds": 7200,
                    "cpu_percent": 7.4,
                    "rss_bytes": 128000000,
                    "players_online": 4,
                    "installed_version": "1.21.8",
                    "restart_required": False,
                    "required_ports_ready": True,
                },
                {
                    "profile_id": "pz-rising",
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
                    "installed_version": "42.13",
                    "restart_required": False,
                    "required_ports_ready": False,
                },
                {
                    "profile_id": "terraria-vanilla",
                    "state": "blocked",
                    "health": "unhealthy",
                    "slot_owner": None,
                    "active_job_id": None,
                    "pid": None,
                    "started_at": None,
                    "uptime_seconds": None,
                    "cpu_percent": None,
                    "rss_bytes": None,
                    "players_online": None,
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
            {"id": "pz-rising", "display_name": "Project Zomboid", "adapter": "systemd", "operations": ["start", "stop", "restart"]},
            {"id": "terraria-tmod", "display_name": "Terraria tModLoader", "adapter": "systemd", "operations": ["start", "stop", "restart", "command"]},
            {"id": "terraria-vanilla", "display_name": "Terraria Vanilla", "adapter": "systemd", "operations": ["start", "stop", "restart", "command"]},
            {"id": "future-game", "display_name": "Future Game", "adapter": "systemd", "operations": ["start", "stop", "restart"]},
        ]
        latest = {"status": status}

        def fulfill(route):
            request = route.request
            path = urlparse(request.url).path
            if path == "/api/v1/session":
                return route.fulfill(json={"actor": "operator@example.test", "csrf_token": "test-token", "expires_at": None})
            if path == "/api/v1/status":
                return route.fulfill(json=latest["status"])
            if path == "/api/v1/perf":
                return route.fulfill(json={
                    "GET /api/v1/status": {"count": 50, "p50_ms": 12.0, "p95_ms": 24.0, "max_ms": 31.0},
                    "rpc": {"count": 50, "p50_ms": 8.0, "p95_ms": 18.0, "max_ms": 22.0},
                    "sse": {"connected_clients": 1, "publish_flush_lag_ms": {"count": 20, "p50_ms": 3.0, "p95_ms": 6.0, "max_ms": 9.0}},
                    "slotd": {"cycle": {"count": 50, "avg_ms": 7.0, "p95_ms": 14.0, "max_ms": 18.0}, "rpc": {"count": 50, "avg_ms": 4.0, "p95_ms": 8.0, "max_ms": 11.0}},
                })
            if path == "/api/v1/profiles":
                return route.fulfill(json=names)
            if path.endswith("/stats/heatmap"):
                return route.fulfill(json={"buckets": [[(day + hour) % 4 for hour in range(24)] for day in range(7)]})
            if path.endswith("/stats/tps"):
                return route.fulfill(json={"samples": [{"timestamp": "2026-07-11T12:00:00Z", "tps": 20, "mspt": 12}]})
            if path == "/api/v1/stream":
                payload = json.dumps(latest["status"], separators=(",", ":"))
                return route.fulfill(
                    status=200,
                    headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
                    body=f"event: status\ndata: {payload}\n\n",
                )
            if path == "/api/v1/profiles/minecraft/logs":
                return route.fulfill(json={"items": [
                    {"timestamp": "2026-07-11T12:00:00Z", "severity": "info", "message": "server ready"},
                    {"timestamp": "2026-07-11T12:01:00Z", "severity": "error", "message": "redacted failure"},
                    {"timestamp": "2026-07-11T12:01:01Z", "severity": "info", "message": "Thread RCON Client /127.0.0.1 started"},
                    {"timestamp": "2026-07-11T12:01:01Z", "severity": "info", "message": "Thread RCON Client /127.0.0.1 shutting down"},
                ], "next_cursor": None})
            if path == "/api/v1/profiles/minecraft/backups":
                return route.fulfill(json={"items": [
                    {"id": "backup-1", "profile_id": "minecraft", "created_at": "2026-07-10T12:00:00Z", "size_bytes": 1073741824, "verified": True, "protected": False},
                ], "next_cursor": None})
            if path.endswith("/stats/summary"):
                profile_id = path.split("/")[4]
                if profile_id == "pz-rising":
                    return route.fulfill(json={
                        "total_hours": 0,
                        "unique_players": 0,
                        "leaderboard": [],
                        "player_tracking": "count",
                        "occupancy": {"latest": 3, "samples": []},
                    })
                return route.fulfill(json={
                    "total_hours": 1,
                    "unique_players": 1,
                    "leaderboard": [{"player": "Guest", "hours": 1, "sessions": 1, "last_seen": "2026-07-11T12:00:00Z"}],
                    "player_tracking": "names",
                    "occupancy": {"latest": 1, "samples": []},
                })
            if path.endswith("/config"):
                profile_id = path.split("/")[4]
                if request.method == "POST":
                    return route.fulfill(json={"profile_id": profile_id, "settings": [], "changed": ["motd"], "restart_required": ["motd"]})
                return route.fulfill(json={"profile_id": profile_id, "settings": [
                    {"key": "motd", "value": "Hello", "type": "str", "bounds": {"max_length": 59}, "restart_required": True},
                    {"key": "max-players", "value": 8, "type": "int", "bounds": {"min": 1, "max": 64}, "restart_required": True},
                    {"key": "pvp", "value": True, "type": "bool", "bounds": {}, "restart_required": True},
                ]})
            if "/stats/" in path:
                return route.fulfill(json={"window": "24h", "samples": [], "buckets": []})
            if path.endswith("/backups"):
                profile_id = path.split("/")[4]
                return route.fulfill(json={"items": [
                    {"id": f"{profile_id}-backup", "profile_id": profile_id, "created_at": "2026-07-10T12:00:00Z", "size_bytes": 1073741824, "verified": True, "protected": False},
                ], "next_cursor": None})
            return route.fulfill(json={"ok": True})

        page.route("**/api/v1/**", fulfill)
        console_errors = []
        page_errors = []
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.goto(web_server)
        page.wait_for_selector('[data-profile-id="minecraft"]', timeout=5000)
        page._dashboard_fixture = latest  # type: ignore[attr-defined]
        yield page
        assert console_errors == []
        assert page_errors == []
        context.close()
        browser.close()


def test_dashboard_contract_and_stable_card_updates(page: Page):
    assert page.locator("main").get_by_role("heading", name="Dashboard").is_visible()
    assert page.get_by_text("active slot", exact=True).is_visible()
    assert page.get_by_role("button", name="Switch server…", exact=True).is_visible()
    assert page.locator('[data-profile-id]').count() == 5
    assert page.locator("#active-slot-title").is_visible()
    assert page.locator("#active-slot-title").inner_text() == "Minecraft"
    assert page.locator("#active-slot-summary").inner_text() == ""
    assert page.locator("#active-manage").get_attribute("href") == "#/servers/minecraft/console"
    assert page.get_by_text("Minecraft", exact=True).count() >= 1
    assert page.get_by_text("Project Zomboid", exact=True).count() >= 1
    assert page.get_by_text("Terraria Vanilla", exact=True).count() >= 1
    assert page.get_by_text("Terraria tModLoader", exact=True).count() >= 1
    assert page.get_by_text("Running", exact=True).is_visible()
    assert page.get_by_text("Stopped", exact=True).is_visible()
    assert page.get_by_text("Blocked", exact=True).is_visible()
    assert page.get_by_text("Failed", exact=True).is_visible()
    assert page.locator("header").count() == 0
    assert page.locator("main").count() == 1
    assert page.locator("aside.sidebar").count() == 1
    assert page.locator("aside.sidebar").evaluate("(node) => Math.round(node.getBoundingClientRect().width)") == 200
    assert page.get_by_role("navigation").get_by_text("Dashboard", exact=True).is_visible()
    assert page.get_by_role("navigation").get_by_text("SERVERS", exact=True).is_visible()
    assert page.get_by_role("navigation").get_by_text("system", exact=True).is_visible()
    assert page.get_by_text("Signed in as operator@example.test", exact=True).is_visible()
    assert page.locator('[aria-live="polite"]').count() >= 2
    assert page.evaluate("Math.min(...[...document.querySelectorAll('button')].filter((node) => node.offsetParent).map((node) => node.getBoundingClientRect().height)) >= 34")
    assert page.get_by_role("button", name="Stop Minecraft", exact=True).is_visible()
    assert page.get_by_role("button", name="Restart Minecraft", exact=True).is_visible()
    assert page.get_by_role("link", name="Manage Project Zomboid").is_visible()
    assert page.locator('[data-profile-id="minecraft"] .cpu-sparkline').is_visible()
    assert page.get_by_role("button", name="Start Project Zomboid", exact=True).is_visible()
    assert page.get_by_role("button", name="Start Terraria Vanilla", exact=True).is_visible()
    assert page.get_by_role("button", name="Start Terraria tModLoader", exact=True).is_visible()
    stopped_button = page.locator('[data-profile-id="pz-rising"] .action-stop')
    assert stopped_button.get_attribute("hidden") is not None
    blocked_start = page.get_by_role("button", name="Start Terraria Vanilla", exact=True)
    assert blocked_start.is_disabled()
    assert "Switch active server" in (blocked_start.get_attribute("title") or "")

    card = page.locator('[data-profile-id="minecraft"]')
    page.evaluate("window.__cardBefore = document.querySelector('[data-profile-id=\"minecraft\"]')")
    page.evaluate(
        """() => window.dispatchEvent(new MessageEvent('game-control-status', {
            data: {generation: 2, profiles: [{profile_id: 'minecraft', state: 'running',
            health: 'healthy', players_online: 9, cpu_percent: 12.5, rss_bytes: 128000000,
            required_ports_ready: true, restart_required: false}]}
        }))"""
    )
    assert page.evaluate("window.__cardBefore === document.querySelector('[data-profile-id=\"minecraft\"]')")
    assert card.get_by_text("9 players", exact=True).is_visible()
    assert page.locator('[data-profile-id="minecraft"] .cpu-sparkline').get_attribute("aria-label") == "CPU usage 12.5 percent"


def test_profile_cards_follow_fallback_order_when_api_reversed(page: Page):
    assert page.locator("#profile-cards").locator("[data-profile-id]").evaluate_all(
        "(cards) => cards.map((card) => card.dataset.profileId)"
    ) == ["minecraft", "terraria-vanilla", "terraria-tmod", "pz-rising", "future-game"]


def test_future_profile_is_appended_after_known_cards(page: Page):
    assert page.locator("#profile-cards").locator("[data-profile-id]").evaluate_all(
        "(cards) => cards.map((card) => card.dataset.profileId)"
    ) == ["minecraft", "terraria-vanilla", "terraria-tmod", "pz-rising", "future-game"]


def test_profile_cards_are_grouped_by_derived_family(page: Page):
    families = page.locator("#profile-cards > [data-profile-family]")
    assert families.evaluate_all(
        """(groups) => groups.map((group) => ({
            family: group.dataset.profileFamily,
            cards: [...group.querySelectorAll('[data-profile-id]')].map((card) => card.dataset.profileId),
            count: group.querySelector('[data-family-count]').textContent,
            owner: group.querySelector('[data-family-owner]').textContent,
            singleton: group.classList.contains('is-singleton'),
        }))"""
    ) == [
        {"family": "minecraft", "cards": ["minecraft"], "count": "1 member", "owner": "Slot: Minecraft", "singleton": True},
        {"family": "terraria", "cards": ["terraria-vanilla", "terraria-tmod"], "count": "2 members", "owner": "Slot: —", "singleton": False},
        {"family": "project-zomboid", "cards": ["pz-rising"], "count": "1 member", "owner": "Slot: —", "singleton": True},
        {"family": "other", "cards": ["future-game"], "count": "1 member", "owner": "Slot: —", "singleton": True},
    ]


def test_family_member_switch_preselects_existing_switch_dialog(page: Page):
    page.get_by_role("button", name="Switch to Terraria Vanilla", exact=True).click()
    dialog = page.get_by_role("dialog", name="Switch active server")
    assert dialog.get_by_label("Target profile", exact=True).input_value() == "terraria-vanilla"
    assert page.locator("#switch-target-summary").inner_text() == "Terraria Vanilla"
    page.keyboard.press("Escape")


def test_sentinel_version_and_null_rss_render_as_unknown_metrics(page: Page):
    page.evaluate(
        """() => window.dispatchEvent(new MessageEvent('game-control-status', {data: {
          generation: 4, profiles: [{profile_id: 'minecraft', state: 'stopped',
            health: 'unknown', slot_owner: null, installed_version: 'False', rss_bytes: null}]
        }}))"""
    )
    card = page.locator('[data-profile-id="minecraft"]')
    assert card.locator(".metric-memory").inner_text() == "—"
    assert card.locator(".metric-version").inner_text() == "—"

    page.goto(f"{page.url}#/servers/minecraft/metrics")
    page.wait_for_selector("#detail-view:not([hidden])")
    assert page.locator("#rail-memory").inner_text() == "—"
    assert page.locator("#metric-memory-current").inner_text() == "—"
    assert page.locator("#rail-version").inner_text() == "—"


def test_blocked_start_is_toasted_without_post(page: Page):
    requests = []
    page.on("request", lambda request: requests.append(request) if request.method == "POST" else None)
    button = page.get_by_role("button", name="Start Project Zomboid", exact=True)
    button.click()
    assert "Switch active server" in page.locator("#toast-region").inner_text()
    assert not any("/profiles/pz-rising/start" in request.url for request in requests)


def test_empty_slot_copy_and_owner_dot(page: Page):
    page.evaluate(
        """() => window.dispatchEvent(new MessageEvent('game-control-status', {
            data: {generation: 3, observed_at: '2026-07-11T12:05:00Z',
            profiles: [
              {profile_id: 'minecraft', state: 'stopped', health: 'unknown', slot_owner: null, cpu_percent: null},
              {profile_id: 'pz-rising', state: 'stopped', health: 'unknown', slot_owner: null, cpu_percent: null},
              {profile_id: 'terraria-vanilla', state: 'stopped', health: 'unknown', slot_owner: null, cpu_percent: null},
              {profile_id: 'terraria-tmod', state: 'stopped', health: 'unknown', slot_owner: null, cpu_percent: null}
            ]}
        }))"""
    )
    assert page.get_by_role("heading", name="Nothing is running", exact=True).is_visible()
    assert page.get_by_text("No server currently owns the active slot.", exact=True).count() >= 1
    assert page.locator(".server-dot.is-owner").count() == 0


def test_sse_patches_dashboard_nodes_and_bounds_cpu_samples(page: Page):
    page.evaluate(
        """() => {
          window.__heroBefore = document.querySelector('#active-slot');
          window.__dotBefore = document.querySelector('[data-profile-nav="minecraft"] .server-dot');
          window.__lineBefore = document.querySelector('[data-profile-id="minecraft"] .cpu-sparkline-line');
          for (let i = 0; i < 140; i++) {
            window.dispatchEvent(new MessageEvent('game-control-status', {data: {
              generation: i + 10, observed_at: '2026-07-11T12:00:00Z',
              profiles: [{profile_id: 'minecraft', state: 'running', health: 'healthy',
                slot_owner: 'minecraft', players_online: i, cpu_percent: i % 101}]
            }}));
          }
        }"""
    )
    assert page.evaluate("window.__heroBefore === document.querySelector('#active-slot')")
    assert page.evaluate("window.__dotBefore === document.querySelector('[data-profile-nav=\"minecraft\"] .server-dot')")
    assert page.evaluate("window.__lineBefore === document.querySelector('[data-profile-id=\"minecraft\"] .cpu-sparkline-line')")
    assert page.locator('[data-profile-id="minecraft"] .metric-players').inner_text() == "139 players"
    assert page.locator('[data-profile-id="minecraft"] .cpu-sparkline-line').evaluate(
        "(node) => node.getAttribute('points').trim().split(/\\s+/).length <= 90"
    )
    assert page.locator("#last-updated").inner_text().startswith("Updated ")


def test_settings_surfaces_one_shot_performance_snapshot_and_browser_marks(page: Page):
    base = page.url.split("#", 1)[0]
    page.goto(f"{base}#/")
    page.get_by_role("button", name="Stop Minecraft", exact=True).click()
    page.evaluate(
        "() => window.dispatchEvent(new MessageEvent('game-control-status', {data: {generation: 9, profiles: [{profile_id: 'minecraft', state: 'stopped', health: 'unknown', slot_owner: null}]}}))"
    )
    assert page.evaluate(
        "() => performance.getEntriesByName('horizon-mutation-click-to-card-reflect', 'measure').length >= 1"
    )

    page.goto(f"{base}#/settings")
    page.wait_for_selector("#performance-panel")
    assert page.get_by_role("heading", name="Performance", exact=True).is_visible()
    assert page.get_by_text("GET /api/v1/status", exact=True).is_visible()
    assert page.get_by_text("p50 12.0 ms · p95 24.0 ms · max 31.0 ms", exact=True).is_visible()
    assert page.get_by_text("Terraria", exact=False).count() >= 1
    assert page.evaluate(
        "() => performance.getEntriesByName('horizon-load-to-first-status-paint', 'measure').length >= 1"
    )


def test_mutations_reflect_transitional_state_before_server_sse(page: Page):
    page.goto(f"{page.url}#/servers/minecraft/console")
    page.wait_for_selector("#detail-view:not([hidden])")
    page.get_by_role("button", name="Stop", exact=True).click()

    assert page.locator('[data-profile-id="minecraft"] .status-text').inner_text() == "Stopping…"
    assert page.locator('[data-profile-id="minecraft"]').get_attribute("class").find("state-stopping") >= 0
    assert page.locator("#detail-status .status-text").text_content() == "Stopping…"
    assert page.locator("#active-slot").get_attribute("class").find("is-transitional") >= 0
    assert page.get_by_role("button", name="Restart", exact=True).is_hidden()
    assert page.evaluate(
        "() => performance.getEntriesByName('horizon-mutation-click-to-optimistic-reflect', 'measure').length >= 1"
    )

    page.evaluate(
        "() => window.dispatchEvent(new MessageEvent('game-control-status', {data: {generation: 99, profiles: [{profile_id: 'minecraft', state: 'stopped', health: 'unknown', slot_owner: null}]}}))"
    )
    assert page.locator('[data-profile-id="minecraft"] .status-text').inner_text() == "Stopped"
    assert page.locator("#detail-status .status-text").text_content() == "Stopped"


def test_first_load_skeleton_contract_and_carbon_shell(page: Page):
    assert page.title() == "Helios Control"
    favicon = page.locator('link[rel="icon"]')
    assert favicon.count() == 1
    assert (favicon.get_attribute("href") or "").startswith("data:image/svg+xml")
    assert page.locator(".content-grid .eyebrow").count() == 0
    assert page.locator("#profile-cards [data-skeleton]").count() == 0


def test_skeleton_placeholders_are_declared_in_shell():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "web" / "styles.css").read_text(encoding="utf-8")
    assert 'class="skeleton-card" data-skeleton' in html
    assert 'class="detail-skeleton" data-detail-skeleton' in html
    assert ".skeleton-block" in css


def test_stopped_profile_sparkline_is_flat_zero(page: Page):
    page.evaluate(
        """() => window.dispatchEvent(new MessageEvent('game-control-status', {data: {
          generation: 300, profiles: [{profile_id: 'minecraft', state: 'stopped',
            health: 'unknown', slot_owner: null, cpu_percent: null}]
        }}))"""
    )
    points = page.locator('[data-profile-id="minecraft"] .cpu-sparkline-line').get_attribute("points")
    assert points
    assert len(points.split()) >= 2
    assert len(set(point.split(",")[1] for point in points.split())) == 1


def test_switch_confirmation_contains_required_summary_and_text(page: Page):
    opener = page.get_by_role("button", name="Switch server…")
    opener.click()
    dialog = page.get_by_role("dialog", name="Switch active server")
    assert dialog.is_visible()
    assert dialog.get_by_text("Current server").is_visible()
    assert dialog.get_by_text("Expected maximum downtime").is_visible()
    assert dialog.get_by_text("Last backup").is_visible()
    assert dialog.get_by_label("Target profile", exact=True).is_visible()
    assert dialog.get_by_label("Type the target profile name to confirm").is_visible()
    assert dialog.get_by_role("button", name="Confirm switch").is_disabled()
    page.keyboard.press("Escape")
    assert dialog.is_hidden()
    assert page.evaluate("document.activeElement === document.querySelector('#switch-active')")


@pytest.mark.parametrize("width,height", [(1280, 900), (390, 844), (375, 812)])
def test_dashboard_has_no_horizontal_overflow(page: Page, width: int, height: int):
    page.set_viewport_size({"width": width, "height": height})
    page.reload()
    page.wait_for_selector('[data-profile-id="minecraft"]', timeout=5000)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert page.locator("main").get_by_role("heading", name="Dashboard").is_visible()


def test_log_filter_and_pause_are_preserved(page: Page):
    page.evaluate("window.__horizonOpenLogs('minecraft')")
    dialog = page.get_by_role("dialog", name="Logs for Minecraft")
    dialog.get_by_label("Severity").select_option("error")
    dialog.get_by_role("button", name="Pause live logs").click()
    page.keyboard.press("Escape")
    page.evaluate("window.__horizonOpenLogs('minecraft')")
    dialog = page.get_by_role("dialog", name="Logs for Minecraft")
    assert dialog.get_by_label("Severity").input_value() == "error"
    assert dialog.get_by_role("button", name="Resume live logs").is_visible()


def test_theme_tokens_persist_and_paper_keeps_console_dark(page: Page):
    expected = {
        "ember": {"--ink": "#e6ebed", "--surface": "#0b0d0e", "--accent": "#8fe53c"},
        "frost": {"--ink": "#eff4f8", "--surface": "#0e141b", "--accent": "#7ab5ee"},
        "moss": {"--ink": "#f0f5ee", "--surface": "#0f1410", "--accent": "#a4c97c"},
        "aurora": {"--ink": "#f2f2f8", "--surface": "#12121b", "--accent": "#b09df0"},
        "paper": {"--ink": "#20242a", "--surface": "#f3f0e9", "--accent": "#2e8f63"},
    }
    page.get_by_role("button", name="Settings").click()
    picker = page.get_by_label("Theme")
    for theme, tokens in expected.items():
        picker.select_option(theme)
        values = page.evaluate(
            """() => {
                const styles = getComputedStyle(document.documentElement);
                const consoleNode = document.querySelector('.log-list');
                return {
                    ink: styles.getPropertyValue('--ink').trim(),
                    surface: styles.getPropertyValue('--surface').trim(),
                    accent: styles.getPropertyValue('--accent').trim(),
                    console: getComputedStyle(consoleNode).backgroundColor,
                };
            }"""
        )
        assert values["ink"] == tokens["--ink"]
        assert values["surface"] == tokens["--surface"]
        assert values["accent"] == tokens["--accent"]
        assert values["console"] in {"rgb(8, 10, 11)", "#080a0b"}
    assert page.evaluate("localStorage.getItem('helios-theme')") == "paper"
    page.reload()
    assert page.evaluate("document.documentElement.dataset.theme") == "paper"


def test_command_palette_opens_with_shortcuts_and_returns_focus(page: Page):
    trigger = page.get_by_role("button", name="Command palette")
    trigger.focus()
    page.keyboard.press("Control+k")
    dialog = page.get_by_role("dialog", name="Command palette")
    assert dialog.is_visible()
    assert page.evaluate("document.activeElement === document.querySelector('#palette-search')")
    page.keyboard.press("Escape")
    assert dialog.is_hidden()
    assert page.evaluate("document.activeElement === document.querySelector('#palette-trigger')")

    page.keyboard.press("Meta+k")
    assert dialog.is_visible()
    close = page.get_by_role("button", name="Close command palette")
    close.focus()
    page.keyboard.press("Shift+Tab")
    assert page.evaluate("document.activeElement?.getAttribute('role') === 'option'")
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement === document.querySelector('#palette-close')")
    close.click()
    assert page.evaluate("document.activeElement === document.querySelector('#palette-trigger')")


def test_command_palette_fuzzy_filters_server_tabs(page: Page):
    page.get_by_role("button", name="Command palette").click()
    search = page.get_by_role("searchbox", name="Filter commands")
    search.fill("terraria-tmod backups")
    options = page.get_by_role("option")
    assert options.count() == 1
    assert options.first.inner_text().startswith("Terraria tModLoader / Backups")


def test_command_palette_arrows_and_enter_navigate_to_server_tab(page: Page):
    page.get_by_role("button", name="Command palette").click()
    search = page.get_by_role("searchbox", name="Filter commands")
    search.fill("terraria-tmod backups")
    search.press("ArrowDown")
    search.press("Enter")
    page.wait_for_url("**/#/servers/terraria-tmod/backups")
    page.wait_for_selector("#detail-view:not([hidden])")
    assert page.get_by_role("heading", name="Terraria tModLoader", exact=True).is_visible()
    assert page.locator("#tab-backups").get_attribute("aria-selected") == "true"


def test_command_palette_switch_uses_existing_confirmation_dialog(page: Page):
    page.get_by_role("button", name="Command palette").click()
    search = page.get_by_role("searchbox", name="Filter commands")
    search.fill("switch terraria tmodloader")
    page.get_by_role("option").first.click()
    switch = page.get_by_role("dialog", name="Switch active server")
    assert switch.is_visible()
    assert switch.get_by_label("Target profile", exact=True).input_value() == "terraria-tmod"
    assert switch.get_by_role("button", name="Confirm switch").is_disabled()
    page.keyboard.press("Escape")
    page.goto(f"{page.url.split('#')[0]}#/")
    page.wait_for_selector('[data-profile-id="minecraft"]', timeout=5000)


def test_mobile_drawer_and_server_group_are_keyboard_accessible(page: Page):
    page.set_viewport_size({"width": 375, "height": 760})
    page.reload()
    page.wait_for_selector('[data-profile-id="minecraft"]', timeout=5000)
    burger = page.get_by_role("button", name="Open navigation")
    assert burger.is_visible()
    assert burger.get_attribute("aria-expanded") == "false"
    burger.focus()
    burger.click()
    drawer = page.locator("#sidebar")
    assert drawer.get_attribute("aria-hidden") == "false"
    page.wait_for_function("document.activeElement === document.querySelector('#drawer-close')")
    assert page.locator("#drawer-overlay").get_attribute("hidden") is None
    assert page.get_by_role("button", name="Close navigation").is_visible()
    assert page.get_by_role("button", name="Servers").get_attribute("aria-expanded") == "true"
    page.get_by_role("button", name="Servers").click()
    assert page.get_by_role("button", name="Servers").get_attribute("aria-expanded") == "false"
    assert page.locator("#server-nav").get_attribute("hidden") is not None
    page.get_by_role("button", name="Close navigation").click()
    assert drawer.get_attribute("aria-hidden") == "true"
    assert page.locator("#drawer-overlay").get_attribute("hidden") is not None
    assert page.evaluate("document.activeElement === document.querySelector('#menu-toggle')")
    burger.click()
    page.locator("#drawer-overlay").click(position={"x": 350, "y": 12})
    assert drawer.get_attribute("aria-hidden") == "true"
    page.keyboard.press("Escape")
    assert page.evaluate("document.activeElement === document.querySelector('#menu-toggle')")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert page.evaluate(
        "Math.min(...[...document.querySelectorAll('button, a, select, input')].filter((node) => node.offsetParent).map((node) => node.getBoundingClientRect().height)) >= 44"
    )


def test_server_detail_route_has_tabs_and_constant_dark_console(page: Page):
    page.goto(f"{page.url}#/servers/minecraft/console")
    page.wait_for_selector("#detail-view:not([hidden])")
    assert page.get_by_role("heading", name="Minecraft", exact=True).is_visible()
    assert page.get_by_text("Servers / Minecraft", exact=True).is_visible()
    tabs = page.get_by_role("tab")
    assert [tabs.nth(i).inner_text() for i in range(tabs.count())] == ["Console", "Metrics", "Stats", "Logs", "Backups", "Config"]
    assert page.get_by_role("tabpanel", name="Console").is_visible()
    console = page.locator("#console-output")
    assert console.is_visible()
    assert page.evaluate("getComputedStyle(document.querySelector('#console-output')).backgroundColor") in {"rgb(8, 10, 11)", "#080a0b"}
    page.get_by_role("tab", name="Console").focus()
    page.keyboard.press("ArrowRight")
    page.wait_for_function(
        "document.querySelector('[role=tab][data-detail-tab=metrics]')?.getAttribute('aria-selected') === 'true'"
    )
    assert page.get_by_role("tab", name="Metrics").get_attribute("aria-selected") == "true"
    assert page.url.endswith("#/servers/minecraft/metrics")


def test_detail_logs_pause_filter_and_backup_restore_prefill(page: Page):
    page.goto(f"{page.url}#/servers/minecraft/logs")
    page.wait_for_selector("#detail-view:not([hidden])")
    assert page.get_by_role("tabpanel", name="Logs").is_visible()
    page.get_by_role("tabpanel", name="Logs").get_by_label("Severity", exact=True).select_option("error")
    page.get_by_role("button", name="Pause live logs").click()
    assert page.get_by_role("button", name="Resume live logs").is_visible()
    page.get_by_role("tab", name="Backups").click()
    page.wait_for_selector("#backup-list")
    restore = page.get_by_role("button", name="Restore backup-1")
    restore.click()
    assert page.get_by_label("Backup ID").input_value() == "backup-1"


def test_detail_command_unsupported_is_honest_and_config_sanitized(page: Page):
    page.goto(f"{page.url}#/servers/minecraft/console")
    page.wait_for_selector("#detail-view:not([hidden])")
    assert "not available" in page.locator("#command-note").inner_text().lower()
    assert page.locator("#command-input").is_disabled()
    page.get_by_role("tab", name="Config").click()
    page.get_by_role("heading", name="Auto-stop").wait_for(state="visible")
    assert page.locator("#idle-stop-enabled").is_visible()
    assert page.locator("#idle-stop-minutes").is_visible()
    page.wait_for_selector("#config-panel [data-config-key]")
    assert page.get_by_role("button", name="Apply changes", exact=True).is_disabled()
    assert not page.locator("#config-panel").inner_text().lower().find("password") >= 0


def test_config_tab_is_typed_diff_apply_and_restart_aware(page: Page):
    page.goto(f"{page.url.split('#')[0]}#/servers/minecraft/config")
    page.wait_for_selector("#config-panel [data-config-key]")
    motd = page.locator('[data-config-key="motd"]')
    motd.fill("New MOTD")
    assert "1 change" in page.locator("#config-diff").inner_text()
    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Apply changes", exact=True).click()
    page.get_by_text("Restart required to take effect", exact=False).wait_for(state="visible")
    assert page.get_by_text("Restart required to take effect", exact=False).is_visible()


def test_stats_are_game_relevant_and_omit_tick_tiles_for_non_minecraft(page: Page):
    base = page.url.split("#")[0]
    page.goto(f"{base}#/servers/minecraft/stats")
    page.wait_for_selector("#detail-view:not([hidden])")
    assert page.get_by_role("heading", name="Minecraft tick telemetry", exact=True).is_visible()

    page.goto(f"{base}#/servers/terraria-tmod/stats")
    page.wait_for_selector("#detail-view:not([hidden])")
    assert not page.get_by_role("heading", name="Minecraft tick telemetry", exact=True).is_visible()
    assert page.get_by_role("heading", name="Leaderboard", exact=True).is_visible()

    page.goto(f"{base}#/servers/pz-rising/stats")
    page.wait_for_function("document.querySelector('#detail-title')?.textContent === 'Project Zomboid'")
    assert not page.get_by_role("heading", name="Minecraft tick telemetry", exact=True).is_visible()
    page.wait_for_selector("#stats-occupancy-block:not([hidden])", timeout=5000)
    assert page.get_by_role("heading", name="Occupancy", exact=True).is_visible()
    assert page.get_by_text("3 online", exact=True).is_visible()
    assert not page.get_by_role("heading", name="Leaderboard", exact=True).is_visible()


def test_detail_command_hint_matches_available_running_profile(page: Page):
    page.evaluate(
        """() => window.dispatchEvent(new MessageEvent('game-control-status', {data: {
          generation: 400, profiles: [{profile_id: 'terraria-vanilla', state: 'running',
            health: 'healthy', slot_owner: 'terraria-vanilla', cpu_percent: 1}]
        }}))"""
    )
    page.goto(f"{page.url}#/servers/terraria-vanilla/console")
    page.wait_for_selector("#detail-view:not([hidden])")
    assert page.locator("#command-input").is_enabled()
    note = page.locator("#command-note").inner_text().lower()
    assert "available" in note
    assert "unavailable" not in note


def test_detail_hash_back_forward_and_sse_keep_tab_focus_and_nodes(page: Page):
    page.goto(f"{page.url}#/servers/minecraft/console")
    page.wait_for_selector("#detail-view:not([hidden])")
    page.get_by_role("tab", name="Logs").click()
    assert page.url.endswith("#/servers/minecraft/logs")
    page.get_by_role("tab", name="Logs").focus()
    page.evaluate("window.__detailBefore = document.querySelector('#detail-view'); window.__logListBefore = document.querySelector('#detail-log-list')")
    page.evaluate(
        """() => window.dispatchEvent(new MessageEvent('game-control-status', {data: {
          generation: 99, profiles: [{profile_id: 'minecraft', state: 'running', health: 'healthy',
          slot_owner: 'minecraft', cpu_percent: 31.2, rss_bytes: 140000000, players_online: 8,
          installed_version: '1.21.8'}]}}))"""
    )
    assert page.evaluate("window.__detailBefore === document.querySelector('#detail-view')")
    assert page.evaluate("window.__logListBefore === document.querySelector('#detail-log-list')")
    assert page.get_by_role("tabpanel", name="Logs").is_visible()
    page.go_back()
    assert page.url.endswith("#/servers/minecraft/console")
    page.go_forward()
    assert page.url.endswith("#/servers/minecraft/logs")


def test_detail_mobile_layout_has_no_overflow(page: Page):
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{page.url}#/servers/minecraft/metrics")
    page.wait_for_selector("#detail-view:not([hidden])")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert page.get_by_role("heading", name="Live rail").is_visible()


def test_375px_layout_audit_and_mobile_evidence(page: Page, tmp_path: Path):
    page.set_viewport_size({"width": 375, "height": 760})
    page.reload()
    base = page.url.split("#")[0]
    page.wait_for_selector('[data-profile-id="minecraft"]', timeout=5000)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert page.locator(".profile-card").evaluate_all(
        "(cards) => cards.every((card) => card.getBoundingClientRect().right <= window.innerWidth)"
    )
    assert page.evaluate(
        "Math.min(...[...document.querySelectorAll('button, a, select, input')].filter((node) => node.offsetParent).map((node) => node.getBoundingClientRect().height)) >= 44"
    )
    page.goto(f"{base}#/servers/minecraft/console")
    page.wait_for_selector("#detail-view:not([hidden])")
    assert page.locator(".detail-tabs").evaluate("(node) => node.scrollWidth > node.clientWidth")
    assert page.locator("#console-output").evaluate("(node) => node.clientWidth <= node.parentElement.clientWidth")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.goto(f"{base}#/servers/minecraft/stats")
    page.wait_for_selector("#detail-view:not([hidden])")
    page.wait_for_selector("#stats-heatmap .heatmap-row")
    assert page.locator("#stats-heatmap").evaluate("(node) => node.scrollWidth > node.clientWidth")
    assert page.locator(".stats-table-wrap").evaluate("(node) => node.scrollWidth > node.clientWidth")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(tmp_path / "horizon-mobile-375x760.png"))

    page.set_viewport_size({"width": 768, "height": 1024})
    page.goto(f"{base}#/")
    page.wait_for_selector('[data-profile-id="minecraft"]', timeout=5000)
    assert page.locator("aside.sidebar").evaluate("(node) => Math.round(node.getBoundingClientRect().width)") == 200
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(tmp_path / "horizon-desktop-768x1024.png"))


def test_accessibility_live_regions_and_reduced_motion(page: Page):
    page.emulate_media(reduced_motion="reduce")
    page.set_viewport_size({"width": 375, "height": 760})
    page.reload()
    page.wait_for_selector('[data-profile-id="minecraft"]', timeout=5000)
    assert page.locator("#toast-region").get_attribute("aria-hidden") == "true"
    assert page.locator("#profile-cards").get_attribute("aria-live") is None
    assert page.locator("#log-list").get_attribute("aria-live") is None
    assert page.locator("#status-announcer").get_attribute("role") == "status"
    live_ids = page.locator('[aria-live="polite"]').evaluate_all("(nodes) => nodes.map((node) => node.id)")
    assert all(live_ids), f"every polite live region needs a stable id: {live_ids}"
    assert len(live_ids) == len(set(live_ids)), f"duplicate live regions announce twice: {live_ids}"
    page.locator("#active-slot").evaluate("(node) => node.classList.add('is-transitional')")
    assert page.locator("#active-slot .slot-mark").evaluate("(node) => getComputedStyle(node).animationName") == "none"
    assert page.locator(".skeleton-block").first.evaluate("(node) => getComputedStyle(node).animationName") == "none"


def test_breakpoint_crossing_resynchronizes_sidebar_state(page: Page):
    page.set_viewport_size({"width": 390, "height": 844})
    page.reload()
    page.wait_for_selector('[data-profile-id="minecraft"]', timeout=5000)
    page.get_by_role("button", name="Open navigation").click()
    page.set_viewport_size({"width": 1280, "height": 900})
    sidebar = page.locator("#sidebar")
    assert sidebar.get_attribute("aria-hidden") == "false"
    assert sidebar.get_attribute("inert") is None
    assert page.locator("#drawer-overlay").get_attribute("hidden") is not None
    page.get_by_role("button", name="Settings").click()
    assert page.get_by_role("heading", name="Settings").is_visible()
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(50)
    assert sidebar.get_attribute("aria-hidden") == "true"
    assert sidebar.get_attribute("inert") == ""
    assert page.get_by_role("button", name="Open navigation").get_attribute("aria-expanded") == "false"


def test_desktop_shell_uses_internal_main_scrolling(page: Page):
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.reload()
    page.wait_for_selector('[data-profile-id="minecraft"]', timeout=5000)
    assert page.locator(".app-shell").evaluate("(node) => node.getBoundingClientRect().height") == 1000
    assert page.locator("main").evaluate("(node) => node.clientHeight") == 1000
    assert page.locator("main").evaluate("(node) => node.scrollHeight > node.clientHeight")
    assert page.evaluate("document.documentElement.scrollHeight <= window.innerHeight + 1")


def test_mobile_servers_breadcrumb_has_touch_target(page: Page):
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{page.url}#/servers/minecraft/metrics")
    page.wait_for_selector("#detail-view:not([hidden])")
    box = page.get_by_role("link", name="Servers", exact=True).bounding_box()
    assert box and box["width"] >= 44 and box["height"] >= 44


def test_aggregate_backups_replaces_rows_without_duplicates(page: Page):
    page.goto(f"{page.url}#/backups")
    page.wait_for_selector("#aggregate-backup-list .backup-row")
    assert page.locator("#aggregate-backup-list .backup-row").count() == 5
    page.goto(f"{page.url}#/")
    page.goto(f"{page.url}#/backups")
    page.wait_for_timeout(100)
    assert page.locator("#aggregate-backup-list .backup-row").count() == 5


def test_detail_metric_history_only_changes_on_status_snapshot(page: Page):
    page.goto(f"{page.url}#/servers/minecraft/metrics")
    page.wait_for_selector("#detail-view:not([hidden])")
    page.wait_for_timeout(100)
    before = page.locator("#metric-cpu-chart .chart-line").get_attribute("points")
    page.goto(f"{page.url}#/servers/minecraft/console")
    page.wait_for_timeout(100)
    page.goto(f"{page.url}#/servers/minecraft/metrics")
    page.wait_for_timeout(100)
    assert page.locator("#metric-cpu-chart .chart-line").get_attribute("points") == before


def test_detail_log_severity_widening_refilters_all_cached_rows(page: Page):
    page.goto(f"{page.url}#/servers/minecraft/logs")
    page.wait_for_selector("#detail-view:not([hidden])")
    logs = page.get_by_role("tabpanel", name="Logs")
    logs.get_by_label("Severity", exact=True).select_option("error")
    assert logs.locator(".log-line").count() == 1
    logs.get_by_label("Severity", exact=True).select_option("all")
    assert logs.locator(".log-line").count() == 2
    assert page.locator("#detail-log-footer").inner_text() == "2 lines · 2 network-noise lines hidden · secrets redacted"


def test_malformed_and_unknown_server_hashes_fall_back_to_dashboard(page: Page):
    page.goto(f"{page.url}#/servers/%zz/console")
    page.wait_for_selector("#dashboard-view:not([hidden])")
    assert page.get_by_role("heading", name="Dashboard").is_visible()
    page.goto(f"{page.url}#/servers/not-a-profile/console")
    page.wait_for_selector("#dashboard-view:not([hidden])")
    assert page.get_by_role("heading", name="Dashboard").is_visible()
