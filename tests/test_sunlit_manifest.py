from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

CLI = Path(__file__).parents[1] / "ops/bin/horizon-sunlit-manifest"


def invoke(tmp_path: Path, *, members=("config/a.txt",), extra=(), destination="overlay.txt", output=None, size_delta=0, archive_hash=None, url="https://example.invalid/artifact.zip"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    archive = tmp_path / "pack.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name in members:
            z.writestr(name, b"sunlit-data")
        for info, data in extra:
            z.writestr(info, data)
    overlay = tmp_path / "overlay"
    overlay.write_bytes(b"overlay")
    out = output or (tmp_path / "manifest.json")
    args = [sys.executable, str(CLI), "--archive", str(archive), "--version", "1.0.0", "--project-id", "123", "--file-id", "456", "--url", url, "--archive-size", str(archive.stat().st_size + size_delta), "--archive-sha256", archive_hash or hashlib.sha256(archive.read_bytes()).hexdigest(), "--overlay-source", str(overlay), "--overlay-destination", destination, "--overlay-sha256", hashlib.sha256(overlay.read_bytes()).hexdigest(), "--output", str(out)]
    return subprocess.run(args, text=True, capture_output=True), out


def test_manifest_is_deterministic_and_mode_0600(tmp_path):
    first, out1 = invoke(tmp_path)
    assert first.returncode == 0, first.stderr
    data = json.loads(out1.read_text())
    assert data["profile_id"] == "minecraft-sunlit-cobblemon"
    assert data["archive"]["entries"] == sorted(data["archive"]["entries"], key=lambda x: x["path"])
    assert data["runtime_policy"]["persistent_dirs"] == ["world", "battle_logs", "easy_npc", "journeymap", "local", "trainers"]
    assert data["runtime_policy"]["mutable_vendor_dirs"] == ["config"]
    assert data["runtime_policy"]["persistent_files"][-2:] == ["ears-debug.log", "rhino.local.properties"]
    assert "ears-debug.log" not in data["runtime_policy"]["required_paths"]
    assert data["runtime_policy"]["empty_mutable_dirs"] == [
        "logs", "crash-reports", "backups", "showdown", "modernfix",
        "moonlight-global-datapacks",
    ]
    assert data["runtime_policy"]["text_overrides"][0]["after_sha256"] == "48ba3ff8cbf05957195774b8e42da656f126ee982fac3aeaec4c23205b2471c8"
    assert out1.stat().st_mode & 0o777 == 0o600
    assert len(data["manifest_sha256"]) == 64


def test_manifest_accepts_structural_directories_but_lists_regular_files_only(tmp_path):
    result, out = invoke(tmp_path, members=("config/", "config/a.txt"))
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text())
    assert data["archive"]["entry_count"] == 1
    assert [entry["path"] for entry in data["archive"]["entries"]] == ["config/a.txt"]


def test_rejects_size_hash_and_metadata_mismatch(tmp_path):
    result, _ = invoke(tmp_path)
    assert result.returncode == 0
    result, _ = invoke(tmp_path / "size", size_delta=1)
    assert result.returncode != 0
    result, _ = invoke(tmp_path / "hash", archive_hash="0" * 64)
    assert result.returncode != 0
    result, _ = invoke(tmp_path / "url", url="http://example.invalid/artifact.zip")
    assert result.returncode != 0
    # Existing output and malformed URL are fail-closed.
    result, out = invoke(tmp_path, output=tmp_path / "manifest.json")
    assert result.returncode != 0 and out.exists()


def test_rejects_unsafe_duplicate_and_overlay_collision(tmp_path):
    for name in ("../escape", "/absolute", "a\\b", "a/./b"):
        result, _ = invoke(tmp_path / name.replace("/", "_").replace("\\", "_"), members=(name,))
        assert result.returncode != 0
    result, _ = invoke(tmp_path / "collision", destination="config/a.txt")
    assert result.returncode != 0
    result, _ = invoke(tmp_path / "dupe", members=("Foo", "foo"))
    assert result.returncode != 0


def test_rejects_symlink_overlay_and_encrypted_member(tmp_path):
    root = tmp_path / "sym"
    root.mkdir()
    archive = root / "pack.zip"
    with zipfile.ZipFile(archive, "w") as z:
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (0o120777 << 16)
        z.writestr(info, "/etc/passwd")
    overlay = root / "overlay"
    overlay.symlink_to(archive)
    args = [sys.executable, str(CLI), "--archive", str(archive), "--version", "1", "--project-id", "1", "--file-id", "1", "--url", "https://example.invalid/x", "--archive-size", str(archive.stat().st_size), "--archive-sha256", hashlib.sha256(archive.read_bytes()).hexdigest(), "--overlay-source", str(overlay), "--overlay-destination", "x", "--overlay-sha256", "0" * 64, "--output", str(root / "out")]
    assert subprocess.run(args, capture_output=True).returncode != 0
