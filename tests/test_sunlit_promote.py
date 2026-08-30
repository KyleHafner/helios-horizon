from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest

from game_control import sunlit_promote as MODULE


ROOT = Path(__file__).parents[1]


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


@contextmanager
def _allow_publication(_action: MODULE.PublicationAction) -> Iterator[None]:
    yield


def test_promotes_exact_inactive_candidate_into_fixed_layout(tmp_path: Path, monkeypatch) -> None:
    stage = tmp_path / "stage"
    candidate = stage / "candidate-v2"
    runtime = candidate / "runtime"
    state = candidate / "state"
    release_root = tmp_path / "opt/releases"
    active = tmp_path / "srv/current"
    state_root = tmp_path / "srv/state"
    libraries = tmp_path / "opt/libraries"
    for path in (runtime, state / "world", state / ".versions/v1/config", state / ".versions/v1/logs", release_root, active.parent, libraries):
        path.mkdir(parents=True, exist_ok=True)
    (runtime / "mods").mkdir()
    (runtime / "mods/new.jar").write_bytes(b"new")
    (runtime / "mods/new.jar").chmod(0o640)
    (state / "world/level.dat").write_bytes(b"world")
    (state / "ops.json").write_bytes(b"ops")
    for name, target in {
        "world": state / "world",
        "ops.json": state / "ops.json",
        "config": state / ".versions/v1/config",
        "logs": state / ".versions/v1/logs",
        "libraries": libraries,
    }.items():
        (runtime / name).symlink_to(target, target_is_directory=target.is_dir())
    document = {
        "manifest_version": 1,
        "profile_id": "minecraft-sunlit-cobblemon",
        "artifact": {"version": "v1", "archive": {"sha256": "a" * 64}},
        "runtime_policy": {
            "persistent_dirs": ["world"],
            "persistent_files": ["ops.json"],
            "required_paths": ["world", "ops.json"],
            "mutable_vendor_dirs": ["config"],
            "empty_mutable_dirs": ["logs"],
            "fixed_symlinks": {"libraries": str(libraries)},
        },
    }
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    manifest_sha = hashlib.sha256(canonical).hexdigest()
    document["manifest_sha256"] = manifest_sha
    manifest = stage / "manifest.json"
    _write_json(manifest, document)
    _write_json(candidate / "candidate.json", {"active": False, "version": "v1", "manifest_sha256": manifest_sha})
    globals_ = MODULE.promote.__globals__
    replacements = {
        "VERSION": "v1",
        "MANIFEST_SHA256": manifest_sha,
        "STAGING_ROOT": stage,
        "CANDIDATE": candidate,
        "MANIFEST": manifest,
        "RELEASE_ROOT": release_root,
        "RELEASE": release_root / "v1",
        "STATE_ROOT": state_root,
        "ACTIVE_LINK": active,
        "SLOT": tmp_path / "slot.json",
        "LIBRARIES": libraries,
    }
    for name, value in replacements.items():
        monkeypatch.setitem(globals_, name, value)
    monkeypatch.setattr(globals_["subprocess"], "run", lambda *_args, **_kwargs: SimpleNamespace(stdout="inactive\n"))
    monkeypatch.setitem(globals_, "_sunlit_ids", lambda: (os.getuid(), os.getgid()))
    guard_depth = 0
    actions: list[MODULE.PublicationAction] = []

    @contextmanager
    def guarded(action: MODULE.PublicationAction) -> Iterator[None]:
        nonlocal guard_depth
        assert guard_depth == 0
        guard_depth += 1
        actions.append(action)
        try:
            yield
        finally:
            guard_depth -= 1

    real_copytree = MODULE.shutil.copytree
    real_fsync_tree = MODULE._fsync_tree

    def copytree_outside_guard(*args, **kwargs):
        assert guard_depth == 0
        return real_copytree(*args, **kwargs)

    def fsync_tree_outside_guard(root: Path) -> None:
        assert guard_depth == 0
        real_fsync_tree(root)

    monkeypatch.setattr(MODULE.shutil, "copytree", copytree_outside_guard)
    monkeypatch.setattr(MODULE, "_fsync_tree", fsync_tree_outside_guard)

    context = replace(MODULE._default_context(), publication_guard=guarded)
    report = MODULE.promote(context)

    assert report["active"] is True
    assert active.is_symlink() and active.resolve() == (release_root / "v1").resolve()
    assert (active / "world/level.dat").read_bytes() == b"world"
    assert (active / "ops.json").read_bytes() == b"ops"
    assert os.readlink(active / "libraries") == str(libraries)
    assert (active / "mods/new.jar").stat().st_mode & 0o777 == 0o644
    assert (state_root / ".horizon/manifest.json").is_file()
    assert not (candidate / "runtime").exists()
    assert not (candidate / "state").exists()
    assert MODULE.promote(context) == report
    assert actions == [
        MODULE.PublicationAction.RELEASE,
        MODULE.PublicationAction.STATE,
        MODULE.PublicationAction.ACTIVE_LINK,
    ]


def test_promote_candidate_keeps_contexts_isolated_under_concurrency(tmp_path: Path, monkeypatch) -> None:
    barrier = threading.Barrier(2)
    observed: list[MODULE.PromotionContext] = []
    guard_events: list[tuple[str, MODULE.PublicationAction]] = []

    def make_guard(label: str) -> MODULE.PublicationGuard:
        @contextmanager
        def guard(action: MODULE.PublicationAction) -> Iterator[None]:
            guard_events.append((label, action))
            yield

        return guard
    def fake_promote(context: MODULE.PromotionContext) -> dict:
        barrier.wait(timeout=2)
        assert context.publication_guard is not None
        with context.publication_guard(MODULE.PublicationAction.ACTIVE_LINK):
            observed.append(context)
        return {
            "active": True,
            "version": context.version,
            "manifest_sha256": context.manifest_sha256,
            "release": str(context.release),
            "active_link": str(context.active_link),
        }

    monkeypatch.setattr(MODULE, "_promote", fake_promote)
    monkeypatch.setattr(MODULE, "RELEASE_ROOT", tmp_path / "releases")
    monkeypatch.setattr(MODULE, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(MODULE, "ACTIVE_LINK", tmp_path / "current")
    monkeypatch.setattr(MODULE, "SLOT", tmp_path / "slot")
    monkeypatch.setattr(MODULE, "LIBRARIES", tmp_path / "libraries")
    original = (
        MODULE.VERSION,
        MODULE.MANIFEST_SHA256,
        MODULE.STAGING_ROOT,
        MODULE.CANDIDATE,
        MODULE.MANIFEST,
        MODULE.RELEASE,
    )

    import concurrent.futures

    calls = (
        ("v1", tmp_path / "s1/manifest.json", tmp_path / "s1/candidate", "a" * 64, make_guard("v1")),
        ("v2", tmp_path / "s2/manifest.json", tmp_path / "s2/candidate", "b" * 64, make_guard("v2")),
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda args: MODULE.promote_candidate(
            version=args[0], manifest=args[1], candidate_root=args[2], manifest_sha256=args[3],
            publication_guard=args[4],
        ), calls))

    assert [result["version"] for result in results] == ["v1", "v2"]
    assert [result["manifest_sha256"] for result in results] == ["a" * 64, "b" * 64]
    assert [result["release"] for result in results] == [str(tmp_path / "releases/v1"), str(tmp_path / "releases/v2")]
    assert [result["active_link"] for result in results] == [str(tmp_path / "current")] * 2
    assert {(context.version, context.manifest_sha256, context.candidate) for context in observed} == {
        ("v1", "a" * 64, tmp_path / "s1/candidate"),
        ("v2", "b" * 64, tmp_path / "s2/candidate"),
    }
    assert set(guard_events) == {
        ("v1", MODULE.PublicationAction.ACTIVE_LINK),
        ("v2", MODULE.PublicationAction.ACTIVE_LINK),
    }
    assert (
        MODULE.VERSION,
        MODULE.MANIFEST_SHA256,
        MODULE.STAGING_ROOT,
        MODULE.CANDIDATE,
        MODULE.MANIFEST,
        MODULE.RELEASE,
    ) == original


def test_promote_candidate_exception_does_not_contaminate_repeated_call(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    def fake_promote(context: MODULE.PromotionContext) -> dict:
        calls.append(context.version)
        if context.version == "bad":
            raise MODULE.PromotionError("synthetic failure")
        return {"active": True, "version": context.version, "release": str(context.release)}

    monkeypatch.setattr(MODULE, "_promote", fake_promote)
    monkeypatch.setattr(MODULE, "RELEASE_ROOT", tmp_path / "releases")
    with pytest.raises(MODULE.PromotionError):
        MODULE.promote_candidate(
            version="bad", manifest=tmp_path / "bad.json", candidate_root=tmp_path / "bad/candidate",
            publication_guard=_allow_publication,
        )
    result = MODULE.promote_candidate(
        version="good", manifest=tmp_path / "good.json", candidate_root=tmp_path / "good/candidate",
        publication_guard=_allow_publication,
    )

    assert calls == ["bad", "good"]
    assert result == {"active": True, "version": "good", "release": str(tmp_path / "releases/good")}


def test_historical_main_rejects_caller_selected_roots(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(MODULE, "MANIFEST", tmp_path / "fixed-manifest.json")
    monkeypatch.setattr(MODULE, "CANDIDATE", tmp_path / "fixed-candidate")
    assert MODULE.main(["--candidate-root", str(tmp_path / "other")]) == 2
    assert MODULE.main(["--manifest", str(tmp_path / "other-manifest.json")]) == 2
    assert MODULE.main([]) == 2


def test_promote_without_publication_guard_fails_before_preflight(monkeypatch) -> None:
    monkeypatch.setattr(MODULE, "_promote", lambda _context: (_ for _ in ()).throw(
        AssertionError("unguarded promotion reached preflight")
    ))
    with pytest.raises(MODULE.PromotionError, match="publication guard is required"):
        MODULE.promote()


@pytest.mark.parametrize("refused", [
    MODULE.PublicationAction.RELEASE,
    MODULE.PublicationAction.STATE,
    MODULE.PublicationAction.ACTIVE_LINK,
])
def test_guard_refusal_preserves_resumable_forward_state(
    tmp_path: Path, monkeypatch, refused: MODULE.PublicationAction,
) -> None:
    candidate = tmp_path / "stage/candidate"
    runtime = candidate / "runtime"
    state = candidate / "state"
    release_root = tmp_path / "releases"
    state_root = tmp_path / "stable/state"
    active_link = tmp_path / "stable/current"
    for path in (runtime, state, release_root, state_root.parent):
        path.mkdir(parents=True, exist_ok=True)
    (state / "sentinel").write_text("unchanged", encoding="utf-8")
    manifest = tmp_path / "stage/manifest.json"
    manifest.write_text("{}", encoding="ascii")
    actions: list[MODULE.PublicationAction] = []
    guard_depth = 0
    refuse = True

    @contextmanager
    def guard(action: MODULE.PublicationAction) -> Iterator[None]:
        nonlocal guard_depth
        assert guard_depth == 0
        actions.append(action)
        if refuse and action is refused:
            raise MODULE.PromotionError("reservation changed")
        guard_depth += 1
        try:
            yield
        finally:
            guard_depth -= 1

    context = MODULE.PromotionContext(
        version="v1",
        manifest_sha256="a" * 64,
        staging_root=candidate.parent,
        candidate=candidate,
        manifest=manifest,
        release_root=release_root,
        release=release_root / "v1",
        state_root=state_root,
        active_link=active_link,
        slot=tmp_path / "slot",
        libraries=tmp_path / "libraries",
        publication_guard=guard,
    )
    document = {
        "manifest_sha256": "a" * 64,
        "artifact": {"version": "v1", "archive": {"sha256": "b" * 64}},
    }
    monkeypatch.setattr(MODULE, "_inactive", lambda _context: None)
    monkeypatch.setattr(MODULE, "_manifest", lambda _context: document)
    monkeypatch.setattr(MODULE, "_trusted_directory", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(
        MODULE,
        "_regular_json",
        lambda path, **_kwargs: {"active": False, "version": "v1", "manifest_sha256": "a" * 64}
        if path.name == "candidate.json" else {},
    )
    monkeypatch.setattr(MODULE, "_sunlit_ids", lambda: (os.getuid(), os.getgid()))
    monkeypatch.setattr(MODULE, "_already_promoted", lambda *_args: None)
    monkeypatch.setattr(MODULE, "_bind_candidate_links", lambda *_args: None)
    monkeypatch.setattr(MODULE, "_normalize_release_permissions", lambda *_args: None)
    monkeypatch.setattr(MODULE, "_write_metadata", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(MODULE, "_prepare_state_ownership", lambda *_args: None)
    monkeypatch.setattr(MODULE, "_verify_release_ownership", lambda *_args: None)
    monkeypatch.setattr(MODULE, "_verify_fresh_state_tree", lambda *_args: None)
    monkeypatch.setattr(MODULE, "_fsync_tree", lambda *_args: (_ for _ in ()).throw(
        AssertionError("fsync-tree must be stubbed before publication")
    ) if guard_depth else None)

    with pytest.raises(MODULE.PromotionError, match="reservation changed"):
        MODULE.promote(context)

    expected_actions = [
        MODULE.PublicationAction.RELEASE,
        MODULE.PublicationAction.STATE,
        MODULE.PublicationAction.ACTIVE_LINK,
    ]
    assert actions == expected_actions[: expected_actions.index(refused) + 1]
    # A sticky refusal is a resumable checkpoint, not a claim of success and
    # not a second attempt through a fence that already denied ownership.
    assert context.release.exists() is not (refused is MODULE.PublicationAction.RELEASE)
    assert state_root.exists() is (refused is MODULE.PublicationAction.ACTIVE_LINK)
    assert not active_link.exists() and not active_link.is_symlink()
    refuse = False
    retry = MODULE.promote(context)
    assert retry["version"] == "v1"
    assert active_link.is_symlink()


@pytest.mark.parametrize("refused", [
    MODULE.PublicationAction.RELEASE,
    MODULE.PublicationAction.VERSION_STATE,
    MODULE.PublicationAction.ACTIVE_LINK,
    MODULE.PublicationAction.METADATA,
])
def test_upgrades_existing_release_without_replacing_stable_state(
    tmp_path: Path, monkeypatch, refused: MODULE.PublicationAction,
) -> None:
    stage = tmp_path / "stage"
    candidate = stage / "candidate"
    runtime = candidate / "runtime"
    candidate_state = candidate / "state"
    release_root = tmp_path / "opt/releases"
    old_release = release_root / "v1"
    new_release = release_root / "v2"
    active = tmp_path / "srv/current"
    state_root = tmp_path / "srv/state"
    libraries = tmp_path / "opt/libraries"
    for path in (
        runtime / "mods",
        candidate_state / "world",
        candidate_state / ".versions/v2/config",
        candidate_state / ".versions/v2/logs",
        old_release,
        state_root / "world",
        state_root / ".versions/v1/config",
        state_root / ".horizon",
        libraries,
    ):
        path.mkdir(parents=True, exist_ok=True)
    (runtime / "mods/new.jar").write_bytes(b"new")
    (candidate_state / "world/level.dat").write_bytes(b"world")
    (candidate_state / "ops.json").write_bytes(b"ops")
    (state_root / "world/level.dat").write_bytes(b"world")
    (state_root / "ops.json").write_bytes(b"ops")
    (candidate_state / "world").chmod(0o755)
    (state_root / "world").chmod(0o750)
    (state_root / ".versions/v1/config/old.toml").write_bytes(b"old")
    _write_json(state_root / ".horizon/manifest.json", {"old": True})
    _write_json(state_root / ".horizon/release.json", {"version": "v1"})
    active.symlink_to(os.path.relpath(old_release, active.parent), target_is_directory=True)
    for name, target in {
        "world": candidate_state / "world",
        "ops.json": candidate_state / "ops.json",
        "config": candidate_state / ".versions/v2/config",
        "logs": candidate_state / ".versions/v2/logs",
        "libraries": libraries,
    }.items():
        (runtime / name).symlink_to(target, target_is_directory=target.is_dir())
    document = {
        "manifest_version": 1,
        "profile_id": "minecraft-sunlit-cobblemon",
        "artifact": {"version": "v2", "archive": {"sha256": "b" * 64}},
        "runtime_policy": {
            "persistent_dirs": ["world"],
            "persistent_files": ["ops.json"],
            "required_paths": ["world", "ops.json"],
            "mutable_vendor_dirs": ["config"],
            "empty_mutable_dirs": ["logs"],
            "fixed_symlinks": {"libraries": str(libraries)},
        },
    }
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    manifest_sha = hashlib.sha256(canonical).hexdigest()
    document["manifest_sha256"] = manifest_sha
    manifest = stage / "manifest.json"
    _write_json(manifest, document)
    _write_json(candidate / "candidate.json", {"active": False, "version": "v2", "manifest_sha256": manifest_sha})
    globals_ = MODULE.promote.__globals__
    for name, value in {
        "VERSION": "v2",
        "MANIFEST_SHA256": manifest_sha,
        "STAGING_ROOT": stage,
        "CANDIDATE": candidate,
        "MANIFEST": manifest,
        "RELEASE_ROOT": release_root,
        "RELEASE": new_release,
        "STATE_ROOT": state_root,
        "ACTIVE_LINK": active,
        "SLOT": tmp_path / "slot.json",
        "LIBRARIES": libraries,
    }.items():
        monkeypatch.setitem(globals_, name, value)
    monkeypatch.setattr(globals_["subprocess"], "run", lambda *_args, **_kwargs: SimpleNamespace(stdout="inactive\n"))
    monkeypatch.setitem(globals_, "_sunlit_ids", lambda: (os.getuid(), os.getgid()))
    actions: list[MODULE.PublicationAction] = []
    guard_depth = 0
    refuse = True

    @contextmanager
    def guarded(action: MODULE.PublicationAction) -> Iterator[None]:
        nonlocal guard_depth
        assert guard_depth == 0
        actions.append(action)
        if refuse and action is refused:
            raise MODULE.PromotionError("reservation changed")
        guard_depth += 1
        try:
            yield
        finally:
            guard_depth -= 1

    real_copytree = MODULE.shutil.copytree
    real_fsync_tree = MODULE._fsync_tree

    def copytree_outside_guard(*args, **kwargs):
        assert guard_depth == 0
        return real_copytree(*args, **kwargs)

    def fsync_tree_outside_guard(root: Path) -> None:
        assert guard_depth == 0
        real_fsync_tree(root)

    monkeypatch.setattr(MODULE.shutil, "copytree", copytree_outside_guard)
    monkeypatch.setattr(MODULE, "_fsync_tree", fsync_tree_outside_guard)

    context = replace(MODULE._default_context(), publication_guard=guarded)
    with pytest.raises(MODULE.PromotionError, match="reservation changed"):
        MODULE.promote(context)
    # Before activation, old active + old metadata remain coherent.  After
    # activation, the retry path repairs metadata and drains candidate state.
    if refused is not MODULE.PublicationAction.METADATA:
        assert active.resolve() == old_release.resolve()
    refuse = False
    report = MODULE.promote(context)

    assert report["version"] == "v2"
    assert active.resolve() == new_release.resolve()
    assert (state_root / "world/level.dat").read_bytes() == b"world"
    assert (state_root / ".versions/v1/config/old.toml").read_bytes() == b"old"
    assert (state_root / ".versions/v2/config").is_dir()
    assert (state_root / ".horizon/release.json").stat().st_mode & 0o777 == 0o640
    assert (new_release / "world").resolve() == (state_root / "world").resolve()
    assert not (candidate / "runtime").exists()
    assert not (candidate / "state").exists()
    assert MODULE.promote(context) == report
    assert active.resolve() == new_release.resolve()


@pytest.mark.parametrize("collision", [
    "root-symlink", "regular-file", "duplicate-source", "unexpected-member", "external-symlink",
])
def test_upgrade_resume_rejects_unverified_production_version_state(
    tmp_path: Path, collision: str,
) -> None:
    release_root = tmp_path / "releases"
    old_release = release_root / "v1"
    new_release = release_root / "v2"
    state_root = tmp_path / "state"
    versions = state_root / ".versions"
    active = tmp_path / "current"
    candidate = tmp_path / "candidate"
    for path in (old_release, new_release, versions, active.parent, candidate):
        path.mkdir(parents=True, exist_ok=True)
    active.symlink_to(os.path.relpath(old_release, active.parent), target_is_directory=True)
    metadata = state_root / ".horizon"
    metadata.mkdir()
    _write_json(metadata / "release.json", {"version": "v1"})
    before = (metadata / "release.json").read_bytes()
    production = versions / "v2"
    outside = tmp_path / "outside"
    outside.mkdir()
    if collision == "root-symlink":
        production.symlink_to(outside, target_is_directory=True)
    elif collision == "regular-file":
        production.write_bytes(b"collision")
    else:
        for path in (production / "config", production / "logs"):
            path.mkdir(parents=True)
            path.chmod(0o750)
        production.chmod(0o750)
        if collision == "duplicate-source":
            source = candidate / "state/.versions/v2"
            (source / "config").mkdir(parents=True)
            (source / "logs").mkdir()
        elif collision == "unexpected-member":
            (production / "unexpected").mkdir()
            (production / "unexpected").chmod(0o750)
        else:
            (production / "config").rmdir()
            (production / "config").symlink_to(outside, target_is_directory=True)
    document = {
        "manifest_sha256": "a" * 64,
        "runtime_policy": {
            "persistent_dirs": [], "persistent_files": [], "required_paths": [],
            "mutable_vendor_dirs": ["config"], "empty_mutable_dirs": ["logs"],
            "fixed_symlinks": {},
        },
    }

    @contextmanager
    def allow(_action: MODULE.PublicationAction) -> Iterator[None]:
        yield

    context = MODULE.PromotionContext(
        version="v2", manifest_sha256="a" * 64,
        staging_root=candidate.parent, candidate=candidate,
        manifest=tmp_path / "manifest.json", release_root=release_root,
        release=new_release, state_root=state_root, active_link=active,
        slot=tmp_path / "slot", libraries=tmp_path / "libraries",
        publication_guard=allow,
    )
    with pytest.raises(MODULE.PromotionError, match="version state|collide"):
        MODULE._resume_upgrade_publication(
            context, candidate / "runtime", candidate / "state", document,
            (os.getuid(), os.getgid()),
        )
    assert active.resolve() == old_release.resolve()
    assert (metadata / "release.json").read_bytes() == before


def test_upgrade_resume_revalidates_version_state_after_publication(
    tmp_path: Path, monkeypatch,
) -> None:
    release_root = tmp_path / "releases"
    old_release = release_root / "v1"
    new_release = release_root / "v2"
    state_root = tmp_path / "state"
    versions = state_root / ".versions"
    active = tmp_path / "current"
    candidate = tmp_path / "candidate"
    candidate_state = candidate / "state"
    for path in (
        old_release, new_release, versions, candidate_state / ".versions/v2/config",
        candidate_state / ".versions/v2/logs",
    ):
        path.mkdir(parents=True, exist_ok=True)
    active.symlink_to(os.path.relpath(old_release, active.parent), target_is_directory=True)
    metadata = state_root / ".horizon"
    metadata.mkdir()
    _write_json(metadata / "release.json", {"version": "v1"})
    before = (metadata / "release.json").read_bytes()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setitem(
        MODULE._prepare_state_ownership.__globals__,
        "_sunlit_ids",
        lambda: (os.getuid(), os.getgid()),
    )
    document = {
        "manifest_sha256": "a" * 64,
        "runtime_policy": {
            "persistent_dirs": [], "persistent_files": [], "required_paths": [],
            "mutable_vendor_dirs": ["config"], "empty_mutable_dirs": ["logs"],
            "fixed_symlinks": {},
        },
    }

    @contextmanager
    def inject_after_move(action: MODULE.PublicationAction) -> Iterator[None]:
        yield
        if action is MODULE.PublicationAction.VERSION_STATE:
            config = versions / "v2/config"
            config.rmdir()
            config.symlink_to(outside, target_is_directory=True)

    context = MODULE.PromotionContext(
        version="v2", manifest_sha256="a" * 64,
        staging_root=candidate.parent, candidate=candidate,
        manifest=tmp_path / "manifest.json", release_root=release_root,
        release=new_release, state_root=state_root, active_link=active,
        slot=tmp_path / "slot", libraries=tmp_path / "libraries",
        publication_guard=inject_after_move,
    )
    with pytest.raises(MODULE.PromotionError, match="version state is unsafe"):
        MODULE._resume_upgrade_publication(
            context, candidate / "runtime", candidate_state, document,
            (os.getuid(), os.getgid()),
        )
    assert active.resolve() == old_release.resolve()
    assert (metadata / "release.json").read_bytes() == before


def test_upgrade_resume_rejects_external_version_state_after_activation(tmp_path: Path) -> None:
    release_root = tmp_path / "releases"
    release = release_root / "v2"
    release.mkdir(parents=True)
    state_root = tmp_path / "state"
    versions = state_root / ".versions"
    versions.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (versions / "v2").symlink_to(outside, target_is_directory=True)
    active = tmp_path / "current"
    active.symlink_to(os.path.relpath(release, active.parent), target_is_directory=True)
    metadata = state_root / ".horizon"
    metadata.mkdir()
    _write_json(metadata / "release.json", {"version": "v1"})
    before = (metadata / "release.json").read_bytes()
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    document = {
        "manifest_sha256": "a" * 64,
        "runtime_policy": {
            "persistent_dirs": [], "persistent_files": [], "required_paths": [],
            "mutable_vendor_dirs": ["config"], "empty_mutable_dirs": ["logs"],
            "fixed_symlinks": {},
        },
    }

    @contextmanager
    def allow(_action: MODULE.PublicationAction) -> Iterator[None]:
        yield

    context = MODULE.PromotionContext(
        version="v2", manifest_sha256="a" * 64,
        staging_root=candidate.parent, candidate=candidate,
        manifest=tmp_path / "manifest.json", release_root=release_root,
        release=release, state_root=state_root, active_link=active,
        slot=tmp_path / "slot", libraries=tmp_path / "libraries",
        publication_guard=allow,
    )
    with pytest.raises(MODULE.PromotionError, match="version state is unsafe"):
        MODULE._resume_upgrade_publication(
            context, candidate / "runtime", candidate / "state", document,
            (os.getuid(), os.getgid()),
        )
    assert active.resolve() == release.resolve()
    assert (metadata / "release.json").read_bytes() == before
