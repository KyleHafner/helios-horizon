from __future__ import annotations

from pathlib import Path
import tomllib

from game_control.deployment_manifest import (
    ABSENT_LIBEXEC_NAMES,
    FIXED_LIBEXEC_NAMES,
    get_manifest,
)


ROOT = Path(__file__).parents[1]
MOVED_PACKAGE_MODULES = {
    "src/game_control/interim_maintenance_control.py",
    "src/game_control/memory_drill.py",
    "src/game_control/phase2_collector.py",
    "src/game_control/phase2_threshold.py",
    "src/game_control/telemetry_migration.py",
}
SOURCE_ONLY_ROOTS = ("tools/acceptance/", "tools/migrations/")


def test_wave6_runtime_projection_has_renamed_runtime_and_no_source_only_tools() -> None:
    manifest = get_manifest()
    sources = set(manifest.runtime_sources)

    assert "src/game_control/maintenance_process.py" in sources
    assert not MOVED_PACKAGE_MODULES & sources
    assert not any(
        source.startswith(prefix)
        for source in sources
        for prefix in SOURCE_ONLY_ROOTS
    )
    assert tuple(
        spec.target.rsplit("/", 1)[-1]
        for spec in manifest.files
        if spec.target.startswith("/usr/local/libexec/")
    ) == FIXED_LIBEXEC_NAMES
    assert set(FIXED_LIBEXEC_NAMES).isdisjoint(ABSENT_LIBEXEC_NAMES)


def test_wave6_wheel_boundary_is_only_the_game_control_source_package() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    wheel = project["tool"]["hatch"]["build"]["targets"]["wheel"]

    assert wheel["packages"] == ["src/game_control"]
    assert (ROOT / "src/game_control/maintenance_process.py").is_file()
    for relative in MOVED_PACKAGE_MODULES:
        assert not (ROOT / relative).exists()
    for relative in (
        "tools/acceptance/performance_probe.py",
        "tools/acceptance/performance_thresholds.py",
        "tools/acceptance/memory_pressure_drill.py",
        "tools/migrations/state_migrate.py",
        "tools/migrations/telemetry_migration.py",
    ):
        assert (ROOT / relative).is_file()
