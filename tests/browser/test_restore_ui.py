from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, expect

from browser_harness import browser_page


@pytest.fixture
def page(web_server):
    with browser_page(viewport={"width": 1280, "height": 900}) as page:
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
                }
            ],
        }
        names = [
            {"id": "minecraft", "display_name": "Minecraft", "adapter": "crafty", "operations": ["start", "stop", "restart"]},
        ]
        latest = {"status": status}
        restore_requests = []
        restore_prepare_failure = {"message": None}

        def fulfill(route):
            request = route.request
            path = urlparse(request.url).path
            if path == "/api/v1/session":
                return route.fulfill(json={"actor": "operator@example.test", "csrf_token": "test-token", "expires_at": None})
            if path == "/api/v1/status":
                return route.fulfill(json=latest["status"])
            if path == "/api/v1/profiles":
                return route.fulfill(json=names)
            if path == "/api/v1/schedules":
                return route.fulfill(json={"schedules": []})
            if path == "/api/v1/stream":
                payload = json.dumps(latest["status"], separators=(",", ":"))
                return route.fulfill(
                    status=200,
                    headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
                    body=f"event: status\ndata: {payload}\n\n",
                )
            if path.endswith("/logs"):
                return route.fulfill(json={"items": [], "next_cursor": None})
            if path.endswith("/backups"):
                return route.fulfill(json={"items": [
                    {
                        "id": "backup-1",
                        "profile_id": "minecraft",
                        "created_at": "2026-07-10T12:00:00Z",
                        "size_bytes": 1073741824,
                        "verified": True,
                        "protected": False,
                    },
                ], "next_cursor": None})
            if path == "/api/v1/profiles/minecraft/restore/prepare":
                restore_requests.append({"path": path, "body": request.post_data_json})
                if restore_prepare_failure["message"]:
                    return route.fulfill(status=409, json={"error": {"message": restore_prepare_failure["message"]}})
                return route.fulfill(json={"confirmation_id": "restore-confirmation-1"})
            if path == "/api/v1/restore/confirm":
                restore_requests.append({"path": path, "body": request.post_data_json})
                return route.fulfill(json={"ok": True})
            return route.fulfill(json={"ok": True})

        page.route("**/api/v1/**", fulfill)
        console_errors = []
        page_errors = []
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.goto(web_server)
        expect(page.locator('[data-profile-id="minecraft"]')).to_be_visible(timeout=5000)
        page._restore_requests = restore_requests  # type: ignore[attr-defined]
        page._restore_prepare_failure = restore_prepare_failure  # type: ignore[attr-defined]
        yield page
        assert [message for message in console_errors if "409 (Conflict)" not in message] == []
        assert page_errors == []


def open_restore(page: Page):
    page.goto(f"{page.url.split('#')[0]}#/servers/minecraft/backups")
    expect(page.locator("#detail-view:not([hidden])")).to_be_visible()
    expect(page.locator("#backup-list")).to_be_visible()
    page.get_by_role("button", name="Restore backup-1", exact=True).click()
    dialog = page.get_by_role("dialog", name="Restore backup")
    expect(dialog).to_be_visible()
    return dialog


def test_restore_confirmation_requires_backup_and_matching_profile(page: Page):
    dialog = open_restore(page)
    backup_id = dialog.get_by_label("Backup ID", exact=True)
    confirmation = dialog.get_by_label("Type the profile name to confirm", exact=True)
    confirm = dialog.get_by_role("button", name="Restore backup", exact=True)

    backup_id.fill("backup-1")
    expect(confirm).to_be_disabled()
    confirmation.fill("not Minecraft")
    expect(confirm).to_be_disabled()
    backup_id.fill("")
    confirmation.fill("minecraft")
    expect(confirm).to_be_disabled()
    backup_id.fill("backup-1")
    confirmation.fill("mInEcRaFt")
    expect(confirm).to_be_enabled()
    expect(dialog).to_be_visible()


def test_restore_success_uses_prepare_then_confirm_and_announces(page: Page):
    open_restore(page)
    dialog = page.get_by_role("dialog", name="Restore backup")
    dialog.get_by_label("Backup ID", exact=True).fill("backup-1")
    dialog.get_by_label("Type the profile name to confirm", exact=True).fill("minecraft")

    prepare_url = "/api/v1/profiles/minecraft/restore/prepare"
    confirm_url = "/api/v1/restore/confirm"
    with page.expect_response(lambda response: response.url.endswith(prepare_url) and response.request.method == "POST") as prepare_info:
        with page.expect_response(lambda response: response.url.endswith(confirm_url) and response.request.method == "POST") as confirm_info:
            dialog.get_by_role("button", name="Restore backup", exact=True).click()

    assert prepare_info.value.ok
    assert confirm_info.value.ok
    requests = page._restore_requests  # type: ignore[attr-defined]
    assert requests == [
        {"path": prepare_url, "body": {"backup_id": "backup-1"}},
        {"path": confirm_url, "body": {"confirmation_id": "restore-confirmation-1"}},
    ]
    expect(dialog).not_to_be_visible()
    expect(page.locator("#status-announcer")).to_have_text("Restore requested for Minecraft.")


def test_restore_prepare_failure_surfaces_error_without_confirming(page: Page):
    page._restore_prepare_failure["message"] = "Backup is not restorable"  # type: ignore[attr-defined]
    dialog = open_restore(page)
    backup_id = dialog.get_by_label("Backup ID", exact=True)
    confirmation = dialog.get_by_label("Type the profile name to confirm", exact=True)
    backup_id.fill("backup-1")
    confirmation.fill("Minecraft")

    with page.expect_response(lambda response: response.url.endswith("/restore/prepare") and response.request.method == "POST"):
        dialog.get_by_role("button", name="Restore backup", exact=True).click()

    expect(page.locator("#status-announcer")).to_have_text("Backup is not restorable")
    expect(dialog).to_be_visible()
    expect(backup_id).to_have_value("backup-1")
    expect(confirmation).to_have_value("Minecraft")
    assert page._restore_requests == [{"path": "/api/v1/profiles/minecraft/restore/prepare", "body": {"backup_id": "backup-1"}}]  # type: ignore[attr-defined]


def test_restore_escape_and_cancel_close_without_api_calls(page: Page):
    open_restore(page)
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog", name="Restore backup")).not_to_be_visible()
    assert page._restore_requests == []  # type: ignore[attr-defined]

    dialog = open_restore(page)
    dialog.get_by_role("button", name="Cancel", exact=True).click()
    expect(page.get_by_role("dialog", name="Restore backup")).not_to_be_visible()
    assert page._restore_requests == []  # type: ignore[attr-defined]


def test_restore_reopening_clears_typed_confirmation(page: Page):
    open_restore(page)
    dialog = page.get_by_role("dialog", name="Restore backup")
    dialog.get_by_label("Backup ID", exact=True).fill("backup-1")
    dialog.get_by_label("Type the profile name to confirm", exact=True).fill("Minecraft")
    expect(dialog.get_by_role("button", name="Restore backup", exact=True)).to_be_enabled()
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog", name="Restore backup")).not_to_be_visible()

    open_restore(page)
    dialog = page.get_by_role("dialog", name="Restore backup")
    confirmation = dialog.get_by_label("Type the profile name to confirm", exact=True)
    confirm = dialog.get_by_role("button", name="Restore backup", exact=True)
    expect(confirmation).to_have_value("")
    dialog.get_by_label("Backup ID", exact=True).fill("backup-1")
    expect(confirm).to_be_disabled()
    confirmation.fill("Minecraft")
    expect(confirm).to_be_enabled()
