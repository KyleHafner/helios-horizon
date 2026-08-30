import hashlib
import stat
import zipfile
from pathlib import Path

import pytest

from game_control.modpack_update import AssemblyError, AssemblySpec, Overlay, TextOverride, activate_release, assemble, assemble_versioned_runtime


def _zip(path: Path, members: list[tuple[str, bytes, int | None]]) -> tuple[int, str, dict]:
    manifest = {}
    with zipfile.ZipFile(path, "w") as z:
        for name, data, mode in members:
            info = zipfile.ZipInfo(name)
            if mode is not None:
                info.create_system = 3
                info.external_attr = mode << 16
            z.writestr(info, data)
            manifest[name] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    raw = path.read_bytes()
    return len(raw), hashlib.sha256(raw).hexdigest(), manifest


def _spec(archive: Path, size: int, digest: str, members: dict, **kw):
    return AssemblySpec(archive, digest, size, members, ("mods", "config", "libraries"), **kw)


def test_curated_assembly_preserves_only_explicit_state_and_is_deterministic(tmp_path: Path):
    archive = tmp_path / "pack.zip"
    size, digest, members = _zip(archive, [("mods/new.jar", b"new", None), ("config/vendor.toml", b"vendor", None)])
    prior = tmp_path / "prior"; (prior / "world").mkdir(parents=True); (prior / "world/level.dat").write_bytes(b"world")
    (prior / "operator.json").write_bytes(b"ops"); (prior / "mods").mkdir(); (prior / "mods/removed.jar").write_bytes(b"stale")
    (prior / "proxy-leftover").write_bytes(b"discard")
    overlay = tmp_path / "local.cfg"; overlay.write_bytes(b"local")
    spec = _spec(archive, size, digest, members, persistent_dirs=("world",), persistent_files=("operator.json",),
                 required_persistent=("world", "operator.json"), overlays=(Overlay(overlay, "config/local.cfg", hashlib.sha256(b"local").hexdigest()),))
    first = assemble(spec, prior, tmp_path / "r1")
    second = assemble(spec, prior, tmp_path / "r2")
    assert first.plan == second.plan
    assert (tmp_path / "r1/world/level.dat").read_bytes() == b"world"
    assert (tmp_path / "r1/operator.json").read_bytes() == b"ops"
    assert not (tmp_path / "r1/mods/removed.jar").exists()
    assert not (tmp_path / "r1/proxy-leftover").exists()
    assert (tmp_path / "r1/config/vendor.toml").read_bytes() == b"vendor"


@pytest.mark.parametrize("name", ["/abs", "../escape", "mods/../escape"])
def test_rejects_unsafe_member_paths(tmp_path: Path, name: str):
    archive = tmp_path / "x.zip"; size, digest, members = _zip(archive, [(name, b"x", None)])
    with pytest.raises(AssemblyError): assemble(_spec(archive, size, digest, members), tmp_path / "p", tmp_path / "r")


def test_rejects_symlink_special_and_duplicate_or_casefold_collision(tmp_path: Path):
    for mode in (stat.S_IFLNK | 0o777, stat.S_IFIFO | 0o644):
        archive = tmp_path / f"{mode}.zip"; size, digest, members = _zip(archive, [("mods/x", b"x", mode)])
        with pytest.raises(AssemblyError): assemble(_spec(archive, size, digest, members), tmp_path / "p", tmp_path / f"r{mode}")
    archive = tmp_path / "collision.zip"
    size, digest, members = _zip(archive, [("mods/X", b"x", None), ("mods/x", b"x", None)])
    with pytest.raises(AssemblyError): assemble(_spec(archive, size, digest, members), tmp_path / "p", tmp_path / "collision")


def test_rejects_archive_and_member_pin_mismatches(tmp_path: Path):
    archive = tmp_path / "x.zip"; size, digest, members = _zip(archive, [("mods/x", b"x", None)])
    with pytest.raises(AssemblyError): assemble(_spec(archive, size + 1, digest, members), tmp_path / "p", tmp_path / "r1")
    bad = dict(members); bad["mods/x"] = {"size": 9, "sha256": "0" * 64}
    with pytest.raises(AssemblyError): assemble(_spec(archive, size, digest, bad), tmp_path / "p", tmp_path / "r2")


def test_rejects_archive_change_during_assembly(tmp_path: Path, monkeypatch):
    archive = tmp_path / "x.zip"; size, digest, members = _zip(archive, [("mods/x", b"x", None)])
    from game_control import modpack_update
    original = modpack_update._digest
    calls = 0
    def changed(path):
        nonlocal calls
        result = original(path)
        if path == archive:
            calls += 1
            if calls > 1:
                return result[0], "0" * 64
        return result
    monkeypatch.setattr(modpack_update, "_digest", changed)
    with pytest.raises(AssemblyError, match="changed during assembly"):
        assemble(_spec(archive, size, digest, members), tmp_path / "p", tmp_path / "r")
    assert not (tmp_path / "r").exists()


def test_rejects_overlay_hash_and_missing_required_state(tmp_path: Path):
    archive = tmp_path / "x.zip"; size, digest, members = _zip(archive, [("mods/x", b"x", None)])
    with pytest.raises(AssemblyError): assemble(_spec(archive, size, digest, members, required_persistent=("world",)), tmp_path / "p", tmp_path / "r1")
    prior = tmp_path / "p"; prior.mkdir(); overlay = tmp_path / "o"; overlay.write_bytes(b"x")
    spec = _spec(archive, size, digest, members, overlays=(Overlay(overlay, "config/o", "0" * 64),))
    with pytest.raises(AssemblyError): assemble(spec, prior, tmp_path / "r2")


def test_rejects_bounds_and_strict_pins(tmp_path: Path):
    archive = tmp_path / "x.zip"; size, digest, members = _zip(archive, [("mods/x", b"x", None), ("mods/y", b"y", None)])
    for field, bound in (("max_entries", 1), ("max_member_size", 0), ("max_total_size", 1), ("max_compression_ratio", 0)):
        kw = {field: bound}
        with pytest.raises(AssemblyError): assemble(_spec(archive, size, digest, members, **kw), tmp_path / "p", tmp_path / field)
    for pin in ({"size": -1, "sha256": "0" * 64}, {"size": 1, "sha256": "A" * 64}, {"size": 1, "sha256": "0"}):
        bad = {"mods/x": pin, "mods/y": members["mods/y"]}
        with pytest.raises(AssemblyError): assemble(_spec(archive, size, digest, bad), tmp_path / "p", tmp_path / f"bad{len(pin)}")


def test_rejects_state_and_overlay_collisions_and_aliases(tmp_path: Path):
    archive = tmp_path / "x.zip"; size, digest, members = _zip(archive, [("config/base", b"x", None)])
    prior = tmp_path / "prior"; prior.mkdir(); (prior / "world").mkdir()
    with pytest.raises(AssemblyError): assemble(_spec(archive, size, digest, members, persistent_dirs=("config",)), prior, tmp_path / "r1")
    overlay = tmp_path / "o"; overlay.write_bytes(b"o"); pin = hashlib.sha256(b"o").hexdigest()
    with pytest.raises(AssemblyError): assemble(_spec(archive, size, digest, members, overlays=(Overlay(overlay, "config/base", pin),)), prior, tmp_path / "r2")
    with pytest.raises(AssemblyError): assemble(_spec(archive, size, digest, members, persistent_dirs=("world",), persistent_files=("world/x",)), prior, tmp_path / "r3")
    with pytest.raises(AssemblyError): assemble(_spec(archive, size, digest, members, overlays=(Overlay(archive, "x", digest),)), prior, tmp_path / "r4")


def test_rejects_undeclared_required_state_manifest_collisions_and_bad_zip(tmp_path: Path):
    archive = tmp_path / "x.zip"; size, digest, members = _zip(archive, [("mods/x", b"x", None)])
    prior = tmp_path / "prior"; prior.mkdir(); (prior / "world").mkdir()
    with pytest.raises(AssemblyError): assemble(_spec(archive, size, digest, members, required_persistent=("world",)), prior, tmp_path / "r1")
    with pytest.raises(AssemblyError):
        assemble(_spec(archive, size, digest, {"mods/X": members["mods/x"], "mods/x": members["mods/x"]}), prior, tmp_path / "r2")
    corrupt = tmp_path / "corrupt.zip"; corrupt.write_bytes(b"not a zip")
    raw = corrupt.read_bytes()
    with pytest.raises(AssemblyError): assemble(_spec(corrupt, len(raw), hashlib.sha256(raw).hexdigest(), {}), prior, tmp_path / "r3")


def test_versioned_runtime_separates_stable_state_and_mutable_vendor(tmp_path: Path):
    vendor = tmp_path / "vendor"; (vendor / "config").mkdir(parents=True); (vendor / "mods").mkdir()
    (vendor / "config/vendor.toml").write_bytes(b"vendor"); (vendor / "mods/new.jar").write_bytes(b"new"); (vendor / "ops.json").write_bytes(b"vendor-ops")
    prior = tmp_path / "prior"; (prior / "world").mkdir(parents=True); (prior / "world/level.dat").write_bytes(b"world"); (prior / "ops.json").write_bytes(b"ops")
    (prior / "mods/removed.jar").parent.mkdir(); (prior / "mods/removed.jar").write_bytes(b"old"); (prior / "unknown").write_bytes(b"discard")
    release = tmp_path / "release"; state = tmp_path / "state"
    libraries = tmp_path / "libraries"; libraries.mkdir()
    before = hashlib.sha256(b"vendor").hexdigest(); after = hashlib.sha256(b"operator").hexdigest()
    report = assemble_versioned_runtime(vendor, state, "v1", prior, release, persistent_dirs=("world",), persistent_files=("ops.json",), required_paths=("world", "ops.json"), mutable_vendor_dirs=("config",), empty_mutable_dirs=("logs",), fixed_symlinks={"libraries": libraries}, text_overrides=(TextOverride("config/vendor.toml", before, after, "vendor", "operator"),))
    assert (release / "world").is_symlink() and (release / "world").resolve() == (state / "world").resolve()
    assert (release / "ops.json").is_symlink() and (release / "config").is_symlink()
    assert (release / "ops.json").read_bytes() == b"ops"
    assert (release / "config/vendor.toml").read_bytes() == b"operator"
    assert (release / "mods/new.jar").exists() and not (release / "mods/removed.jar").exists()
    assert (release / "logs").is_symlink() and (release / "logs").resolve() == (state / ".versions/v1/logs").resolve()
    assert (release / "libraries").is_symlink() and (release / "libraries").resolve() == libraries.resolve()
    assert not (state / "unknown").exists()
    assert report.overlays == ("config", "libraries", "logs")


def test_versioned_runtime_rejects_partial_existing_state_and_cleans_failure(tmp_path: Path):
    vendor = tmp_path / "vendor"; (vendor / "config").mkdir(parents=True); (vendor / "config/x").write_bytes(b"x")
    prior = tmp_path / "prior"; prior.mkdir(); state = tmp_path / "state"; state.mkdir(); release = tmp_path / "release"
    with pytest.raises(AssemblyError): assemble_versioned_runtime(vendor, state, "v1", prior, release, persistent_dirs=("world",), required_paths=("world",), mutable_vendor_dirs=("config",))
    assert not release.exists()
    state2 = tmp_path / "state2"; (prior / "world").mkdir(); (prior / "world/x").write_bytes(b"x")
    with pytest.raises(AssemblyError): assemble_versioned_runtime(vendor, state2, "v1", prior, tmp_path / "release2", persistent_dirs=("world",), required_paths=("world",), mutable_vendor_dirs=("missing",))
    assert not state2.exists() and not (tmp_path / "release2").exists()


def test_versioned_runtime_rejects_unsafe_versions_root_and_overlaps(tmp_path: Path):
    vendor = tmp_path / "vendor"; (vendor / "config").mkdir(parents=True); (vendor / "config/x").write_bytes(b"x")
    prior = tmp_path / "prior"; (prior / "world").mkdir(parents=True); (prior / "world/x").write_bytes(b"x")
    state = tmp_path / "state"; state.mkdir(); (state / "world").mkdir(); (state / ".versions").symlink_to(tmp_path)
    with pytest.raises(AssemblyError):
        assemble_versioned_runtime(vendor, state, "v1", prior, tmp_path / "release", persistent_dirs=("world",), required_paths=("world",))
    with pytest.raises(AssemblyError):
        assemble_versioned_runtime(vendor, tmp_path / "state2", "nested/v1", prior, tmp_path / "release2", persistent_dirs=("world",), required_paths=("world",))
    with pytest.raises(AssemblyError):
        assemble_versioned_runtime(vendor, tmp_path / "state3", "v1", prior, tmp_path / "release3", persistent_dirs=("world",), required_paths=("world",), mutable_vendor_dirs=("config", "config/nested"))
    with pytest.raises(AssemblyError):
        assemble_versioned_runtime(vendor, tmp_path / "state4", "v1", prior, tmp_path / "release4", persistent_dirs=("world",), required_paths=("world",), empty_mutable_dirs=("world",))
    unsafe_target = tmp_path / "unsafe-target"; unsafe_target.symlink_to(vendor)
    with pytest.raises(AssemblyError):
        assemble_versioned_runtime(vendor, tmp_path / "state5", "v1", prior, tmp_path / "release5", persistent_dirs=("world",), required_paths=("world",), fixed_symlinks={"libraries": unsafe_target})
    with pytest.raises(AssemblyError):
        assemble_versioned_runtime(vendor, tmp_path / "state6", "v1", prior, tmp_path / "release6", persistent_dirs=("world",), required_paths=("world",), text_overrides=(TextOverride("mods/x", "0" * 64, "1" * 64, "a", "b"),))


def test_activation_is_atomic_and_bound_to_expected_prior(tmp_path: Path):
    releases = tmp_path / "releases"; old = releases / "old"; new = releases / "new"
    old.mkdir(parents=True); new.mkdir(); active = tmp_path / "current"; active.symlink_to("releases/old", target_is_directory=True)
    assert activate_release(active, releases, new, expected_prior="releases/old") == "releases/old"
    assert active.resolve() == new.resolve()
    with pytest.raises(AssemblyError, match="changed before activation"):
        activate_release(active, releases, old, expected_prior="releases/not-current")
    assert active.resolve() == new.resolve()


def test_activation_rejects_targets_outside_release_root(tmp_path: Path):
    releases = tmp_path / "releases"; releases.mkdir(); outside = tmp_path / "outside"; outside.mkdir()
    with pytest.raises(AssemblyError, match="outside"):
        activate_release(tmp_path / "current", releases, outside, expected_prior=None)
