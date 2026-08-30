from __future__ import annotations

import argparse
import re
from pathlib import Path

from game_control.cli import build_parser
from game_control.deployment_manifest import get_manifest
from ops.install import Installer


ROOT = Path(__file__).parents[1]
MILESTONE = re.compile(r"(?i)\b(?:phase[ _.-]?[1-4]|interim)\b")
MOVED_RUNTIME_NAMES = {
    "interim_maintenance_control.py",
    "memory_drill.py",
    "phase2_collector.py",
    "phase2_threshold.py",
    "telemetry_migration.py",
}


def _command_names(parser: argparse.ArgumentParser) -> set[str]:
    names: set[str] = set()
    pending = [parser]
    while pending:
        current = pending.pop()
        for action in current._actions:
            if isinstance(action, argparse._SubParsersAction):
                names.update(action.choices)
                pending.extend(action.choices.values())
    return names


def test_installed_projection_has_no_milestone_or_moved_runtime_vocabulary() -> None:
    manifest = get_manifest()
    source_paths = {
        spec.source for spec in (*manifest.files, *manifest.runtime_support)
    } | set(manifest.runtime_sources) | {"scripts/verify-deployed.py"}
    findings: dict[str, list[str]] = {}
    for relative in sorted(source_paths):
        text = (ROOT / relative).read_text(encoding="utf-8")
        matches = sorted(set(match.group(0) for match in MILESTONE.finditer(text)))
        if matches:
            findings[relative] = matches

    assert findings == {}
    assert not {
        Path(source).name for source in manifest.runtime_sources
    } & MOVED_RUNTIME_NAMES
    assert not MILESTONE.search(" ".join(_command_names(build_parser())))


def test_fresh_alternate_root_contains_only_current_runtime_and_helpers(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    installer = Installer(root, skip_systemd_verify=True)
    installer.apply()

    runtime = root / "opt/game-control/src/game_control"
    assert (runtime / "maintenance_process.py").is_file()
    assert not MOVED_RUNTIME_NAMES & {path.name for path in runtime.iterdir()}
    actual_helpers = {
        path.name for path in (root / "usr/local/libexec").iterdir()
        if path.is_file()
    }
    expected_helpers = {
        spec.target.rsplit("/", 1)[-1]
        for spec in get_manifest().files
        if spec.target.startswith("/usr/local/libexec/")
    }
    assert actual_helpers == expected_helpers


def test_historical_swagbench_import_note_is_explicitly_unsupported() -> None:
    note = (ROOT / "docs/swagbench-history-import.md").read_text(encoding="utf-8")
    prose = " ".join(note.split())

    assert "/usr/local/libexec/horizon-benchmark-import" not in note
    assert "does not ship or install a SwagBench history importer" in prose
    assert "there is no supported command or operator procedure" in prose
    assert "run:" not in prose.lower()


def test_allowed_historical_schemas_and_deferred_tuning_paths_remain_explicit() -> None:
    thresholds = (ROOT / "tools/acceptance/performance_thresholds.py").read_text()
    probe = (ROOT / "tools/acceptance/performance_probe.py").read_text()
    tuning = (ROOT / "ops/bin/horizon-jvm-args").read_text()

    assert 'CONTRACT_VERSION = "phase2.1.v1"' in thresholds
    assert '"schemaVersion": "phase2.1.inputs.v1"' in probe
    assert "/var/lib/game-control/phase3/latest-summary.json" in tuning
    assert "/var/lib/game-control/phase3/candidate.args" in tuning
    assert "/var/lib/game-control/phase3/accepted.digest" in tuning
    assert all(
        spec.source != "ops/bin/horizon-jvm-args"
        for spec in get_manifest().files
    )
