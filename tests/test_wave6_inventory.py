from __future__ import annotations

import os
from pathlib import Path

from game_control.deployment_manifest import (
    ABSENT_LIBEXEC_NAMES,
    FIXED_LIBEXEC_NAMES,
    get_manifest,
)
from ops.install import main as installer_main


FIXED_HELPERS = (
    "game-slot-run",
    "game-console-stop",
    "game-console-command",
    "game-sunlit-prepare",
    "game-sunlit-rcon-prepare",
    "game-sunlit-stop",
    "horizon-alert-notify",
    "horizon-bore-liveness",
    "horizon-lazymc-wake",
    "horizon-sunlit-auto-update",
    "horizon-sunlit-update-rpc",
)
ABSENT_HELPERS = (
    "horizon-sunlit-manifest",
    "horizon-sunlit-stage",
    "horizon-sunlit-promote",
    "horizon_journal.py",
    "horizon-state-migrate",
    "horizon-telemetry-migrate",
    "horizon-memory-drill",
    "horizon-phase2-threshold",
    "horizon-phase2-collect",
    "horizon-phase2-browser-evidence",
    "horizon-phase2-live-acceptance",
)
PERMITTED_COMPATIBILITY_NAMES = {
    "horizon-backup-reconcile",
    "horizon-capability-issue",
    "horizon-jvm-args",
    "horizon-session-revoke-all",
    "horizon-journal-evidence",
    "horizon-journal-finalize",
}
FIXED_SYSTEMD_UNITS = (
    "game-control-web.service",
    "game-slotd.service",
    "horizon-bore-liveness.service",
    "horizon-bore-liveness.timer",
    "horizon-sunlit-auto-update.service",
    "horizon-sunlit-auto-update.timer",
    "lazymc-minecraft.service",
    "bore-minecraft-fenced.service",
    "horizon-alert-drill@.service",
    "horizon-alert-notify@.service",
    "horizon-terraria-relay.service",
    "minecraft-sunlit-cobblemon.service",
    "terraria-tmod.service",
    "terraria-vanilla.service",
)
FIXED_SLICES = ("games.slice", "horizon.slice", "maintenance.slice")
FIXED_DROPINS = (
    "game-slotd.service.d/io-metrics.conf",
    "minecraft-sunlit-cobblemon.service.d/gc-telemetry.conf",
)


def test_post_wave5_helper_inventory_is_exact_and_alias_free() -> None:
    manifest = get_manifest()
    projected = tuple(
        spec.target.removeprefix("/usr/local/libexec/")
        for spec in manifest.files
        if spec.target.startswith("/usr/local/libexec/")
    )

    assert FIXED_LIBEXEC_NAMES == FIXED_HELPERS
    assert ABSENT_LIBEXEC_NAMES == ABSENT_HELPERS
    assert projected == FIXED_HELPERS
    assert not set(projected) & set(ABSENT_HELPERS)
    assert not set(projected) & PERMITTED_COMPATIBILITY_NAMES


def test_post_wave5_systemd_and_peer_boundaries_are_frozen() -> None:
    manifest = get_manifest()
    systemd = {
        spec.target.removeprefix("/etc/systemd/system/")
        for spec in manifest.files
        if spec.target.startswith("/etc/systemd/system/")
    }
    namespace = next(item for item in manifest.namespaces if item.name == "systemd")
    libexec = next(item for item in manifest.namespaces if item.name == "libexec")

    assert systemd == set(FIXED_SYSTEMD_UNITS + FIXED_SLICES + FIXED_DROPINS)
    assert namespace.exact == FIXED_SYSTEMD_UNITS + FIXED_SLICES
    assert namespace.prefixes == (
        "game-control-", "game-slotd", "horizon-", "lazymc-",
        "bore-minecraft-fenced",
    )
    assert libexec.exact == FIXED_HELPERS
    assert libexec.prefixes == ("game-", "horizon-")


def test_alternate_root_apply_and_check_preserve_unmanaged_old_helpers(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    libexec = root / "usr/local/libexec"
    libexec.mkdir(parents=True)
    preserved = {
        libexec / "horizon-jvm-args": b"operator-owned compatibility file\n",
        libexec / "horizon-phase2-collect": b"operator-owned retired file\n",
    }
    for path, payload in preserved.items():
        path.write_bytes(payload)
        path.chmod(0o755)

    assert installer_main([
        "--apply", "--root", str(root), "--skip-systemd-verify",
    ]) == 0
    secret = root / "etc/game-control/secrets.d/horizon-b2-rclone.conf"
    secret.write_bytes(b"offline fixture\n")
    os.chmod(secret, 0o600)
    assert installer_main([
        "--check", "--root", str(root), "--skip-systemd-verify",
    ]) == 0
    assert installer_main([
        "--apply", "--root", str(root), "--skip-systemd-verify",
    ]) == 0

    for path, payload in preserved.items():
        assert path.read_bytes() == payload
        assert path.stat().st_mode & 0o777 == 0o755
