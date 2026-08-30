from __future__ import annotations

import json
import runpy
from pathlib import Path


ROOT = Path(__file__).parents[1]
AUTO = ROOT / "ops/bin/horizon-sunlit-auto-update"


def test_discovers_latest_exact_official_server_pack(monkeypatch) -> None:
    helper = runpy.run_path(str(AUTO), run_name="horizon-sunlit-auto-update")
    calls = []

    def fetch(url: str):
        calls.append(url)
        if url.endswith("/files"):
            return {
                "data": [
                    {"id": 10, "releaseType": 2, "fileStatus": 4, "hasServerPack": True, "gameVersions": ["1.20.1", "Forge"]},
                    {"id": 20, "releaseType": 1, "fileStatus": None, "hasServerPack": True, "gameVersions": ["1.20.1", "Forge"]},
                ]
            }
        return {
            "data": [
                {
                    "id": 21,
                    "fileName": "SERVER-PACK-Society-Sunlit-Cobblemon-1.2.3-SSV4.1.4.zip",
                    "fileLength": 123456,
                }
            ]
        }

    monkeypatch.setitem(helper["discover"].__globals__, "_fetch_json", fetch)
    release = helper["discover"]()

    assert release["version"] == "1.2.3-SSV4.1.4"
    assert release["file_id"] == "21"
    assert calls[-1].endswith("/files/20/additional-files")


def test_check_reports_current_without_mutation(tmp_path: Path, monkeypatch) -> None:
    helper = runpy.run_path(str(AUTO), run_name="horizon-sunlit-auto-update")
    state = tmp_path / "state/.horizon"
    state.mkdir(parents=True)
    (state / "release.json").write_text(json.dumps({"version": "v2"}), encoding="utf-8")
    globals_ = helper["run"].__globals__
    monkeypatch.setitem(globals_, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setitem(globals_, "discover", lambda: {"version": "v2"})
    monkeypatch.setitem(globals_, "_stage", lambda _release: (_ for _ in ()).throw(AssertionError("must not stage")))

    assert helper["run"](check_only=False) == {"state": "current", "installed": "v2", "available": None}


def test_inactive_gate_defers_before_staging(monkeypatch) -> None:
    helper = runpy.run_path(str(AUTO), run_name="horizon-sunlit-auto-update")
    globals_ = helper["run"].__globals__
    monkeypatch.setitem(globals_, "discover", lambda: {"version": "v2", "file_id": "2"})
    monkeypatch.setitem(globals_, "_installed_version", lambda: "v1")
    monkeypatch.setitem(globals_, "_inactive", lambda: False)
    monkeypatch.setitem(globals_, "_stage", lambda _release: (_ for _ in ()).throw(AssertionError("must not stage")))
    monkeypatch.setattr(globals_["os"], "geteuid", lambda: 0)

    assert helper["run"](check_only=False) == {"state": "deferred", "installed": "v1", "available": "v2"}


def test_systemd_timer_and_installer_are_wired() -> None:
    service = (ROOT / "ops/systemd/horizon-sunlit-auto-update.service").read_text(encoding="utf-8")
    timer = (ROOT / "ops/systemd/horizon-sunlit-auto-update.timer").read_text(encoding="utf-8")
    assert "ExecStart=/usr/local/libexec/horizon-sunlit-auto-update" in service
    assert "TimeoutStartSec=4h" in service
    assert "OnCalendar=*-*-* 05:00:00 America/New_York" in timer
    assert "Persistent=true" in timer
    from game_control.deployment_manifest import get_manifest

    helper_sources = {
        spec.source.rsplit("/", 1)[-1]
        for spec in get_manifest().files
        if spec.target.startswith("/usr/local/libexec/")
    }
    assert {
        "horizon-sunlit-auto-update",
        "horizon-sunlit-update-rpc",
        "horizon-sunlit-manifest",
        "horizon-sunlit-stage",
    } <= helper_sources
