from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).parents[1]
CLI = ROOT / "ops/bin/horizon-sunlit-stage"


def _manifest(tmp_path: Path):
    archive = tmp_path / "pack.zip"
    config = b"setting = 10\n"
    mod = b"mod"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("config/base.toml", config)
        output.writestr("mods/new.jar", mod)
    overlay = tmp_path / "metrics.jar"; overlay.write_bytes(b"metrics")
    libraries = tmp_path / "libraries"; libraries.mkdir()
    entries = []
    with zipfile.ZipFile(archive) as source:
        for info in source.infolist():
            payload = source.read(info)
            entries.append({"path": info.filename, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
    prior = tmp_path / "prior"; (prior / "world").mkdir(parents=True); (prior / "world/data").write_bytes(b"world")
    (prior / "ops.json").write_bytes(b"ops")
    updated = b"setting = 15\n"
    document = {
        "manifest_version": 1,
        "profile_id": "minecraft-sunlit-cobblemon",
        "artifact": {"version": "v1", "archive": {"size": archive.stat().st_size, "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}},
        "archive": {"roots": ["config", "mods"], "entries": entries},
        "overlay": {"source": str(overlay), "destination": "mods/metrics.jar", "sha256": hashlib.sha256(b"metrics").hexdigest()},
        "runtime_policy": {
            "persistent_dirs": ["world"], "persistent_files": ["ops.json"], "required_paths": ["world", "ops.json"],
            "mutable_vendor_dirs": ["config"], "empty_mutable_dirs": ["logs"],
            "fixed_symlinks": {"libraries": str(libraries)},
            "text_overrides": [{"path": "config/base.toml", "before_sha256": hashlib.sha256(config).hexdigest(), "after_sha256": hashlib.sha256(updated).hexdigest(), "old": "setting = 10", "new": "setting = 15"}],
        },
    }
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    document["manifest_sha256"] = hashlib.sha256(canonical).hexdigest()
    manifest = tmp_path / "manifest.json"; manifest.write_text(json.dumps(document))
    return archive, prior, manifest


def test_stages_complete_inactive_candidate(tmp_path: Path):
    archive, prior, manifest = _manifest(tmp_path)
    candidate = tmp_path / "candidate"
    result = subprocess.run([sys.executable, str(CLI), "--manifest", str(manifest), "--archive", str(archive), "--prior-runtime", str(prior), "--candidate-root", str(candidate)], cwd=ROOT, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["active"] is False
    assert (candidate / "runtime/world/data").read_bytes() == b"world"
    assert (candidate / "runtime/ops.json").read_bytes() == b"ops"
    assert (candidate / "runtime/config/base.toml").read_bytes() == b"setting = 15\n"
    assert (candidate / "runtime/mods/metrics.jar").read_bytes() == b"metrics"
    assert (candidate / "runtime/logs").is_symlink()
    assert not (candidate / "vendor-release").exists()
    assert report["vendor_retained"] is False


def test_rejects_tampered_manifest_and_cleans_candidate(tmp_path: Path):
    archive, prior, manifest = _manifest(tmp_path)
    document = json.loads(manifest.read_text()); document["artifact"]["version"] = "tampered"; manifest.write_text(json.dumps(document))
    candidate = tmp_path / "candidate"
    result = subprocess.run([sys.executable, str(CLI), "--manifest", str(manifest), "--archive", str(archive), "--prior-runtime", str(prior), "--candidate-root", str(candidate)], cwd=ROOT, text=True, capture_output=True)
    assert result.returncode != 0
    assert not candidate.exists()
