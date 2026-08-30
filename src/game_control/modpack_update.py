"""Fail-closed, pure filesystem assembly for pinned Sunlit-style modpacks.

This module deliberately stops at producing a verified release directory.  It
does not select a release, mutate a live symlink, or start a service.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

MAX_ARCHIVE_ENTRIES = 100_000
MAX_MEMBER_SIZE = 2 * 1024 * 1024 * 1024
MAX_TOTAL_SIZE = 8 * 1024 * 1024 * 1024
MAX_COMPRESSION_RATIO = 1_000


class AssemblyError(ValueError):
    """An input failed a fail-closed assembly check."""


@dataclass(frozen=True)
class FilePin:
    path: str
    size: int
    sha256: str

    @classmethod
    def from_value(cls, path: str, value: Any) -> "FilePin":
        if isinstance(value, Mapping):
            try:
                size = int(value["size"]); digest = value["sha256"]
            except (KeyError, TypeError, ValueError) as exc:
                raise AssemblyError(f"invalid manifest entry: {path}") from exc
            if size < 0 or size > MAX_MEMBER_SIZE or not isinstance(digest, str) or len(digest) != 64 or digest != digest.lower() or any(c not in "0123456789abcdef" for c in digest):
                raise AssemblyError(f"invalid manifest pin: {path}")
            return cls(path, size, digest)
        raise AssemblyError(f"invalid manifest entry: {path}")


@dataclass(frozen=True)
class Overlay:
    source: Path
    destination: str
    sha256: str


@dataclass(frozen=True)
class TextOverride:
    path: str
    before_sha256: str
    after_sha256: str
    old: str
    new: str


@dataclass(frozen=True)
class AssemblySpec:
    archive: Path
    archive_sha256: str
    archive_size: int
    members: Mapping[str, Any]
    vendor_roots: Sequence[str]
    persistent_dirs: Sequence[str] = ()
    persistent_files: Sequence[str] = ()
    required_persistent: Sequence[str] = ()
    overlays: Sequence[Overlay] = ()
    max_entries: int = MAX_ARCHIVE_ENTRIES
    max_member_size: int = MAX_MEMBER_SIZE
    max_total_size: int = MAX_TOTAL_SIZE
    max_compression_ratio: int = MAX_COMPRESSION_RATIO


@dataclass(frozen=True)
class AssemblyReport:
    release: Path
    files: tuple[str, ...]
    preserved: tuple[str, ...]
    overlays: tuple[str, ...]
    plan: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"release": str(self.release), "files": list(self.files),
                "preserved": list(self.preserved), "overlays": list(self.overlays),
                "plan": list(self.plan)}


def activate_release(
    active_link: Path,
    release_root: Path,
    release: Path,
    *,
    expected_prior: str | None,
) -> str | None:
    """Atomically select one verified release while refusing pointer races."""
    active_link = Path(active_link); release_root = Path(release_root); release = Path(release)
    if release_root.is_symlink() or not release_root.is_dir():
        raise AssemblyError("release root is unsafe")
    if active_link.parent.is_symlink() or not active_link.parent.is_dir():
        raise AssemblyError("active-link parent is unsafe")
    if release.is_symlink() or not release.is_dir() or release.parent.resolve() != release_root.resolve():
        raise AssemblyError("release target is outside the reviewed root")
    if active_link.exists() and not active_link.is_symlink():
        raise AssemblyError("active path is not a symlink")
    try:
        prior = os.readlink(active_link)
    except FileNotFoundError:
        prior = None
    if prior != expected_prior:
        raise AssemblyError("active release changed before activation")
    target = os.path.relpath(release, active_link.parent)
    temporary = active_link.parent / f".{active_link.name}.{uuid.uuid4().hex}"
    swapped = False
    try:
        os.symlink(target, temporary, target_is_directory=True)
        dirfd = os.open(active_link.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try: os.fsync(dirfd)
        finally: os.close(dirfd)
        os.replace(temporary, active_link); swapped = True
        dirfd = os.open(active_link.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try: os.fsync(dirfd)
        finally: os.close(dirfd)
        return prior
    except BaseException:
        temporary.unlink(missing_ok=True)
        if swapped:
            rollback = active_link.parent / f".{active_link.name}.rollback.{uuid.uuid4().hex}"
            try:
                if prior is None:
                    active_link.unlink(missing_ok=True)
                else:
                    os.symlink(prior, rollback, target_is_directory=True)
                    os.replace(rollback, active_link)
                dirfd = os.open(active_link.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try: os.fsync(dirfd)
                finally: os.close(dirfd)
            finally:
                rollback.unlink(missing_ok=True)
        raise


def _digest(path: Path) -> tuple[int, str]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            h.update(chunk)
    return size, h.hexdigest()


def _valid_sha256(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(ch in "0123456789abcdef" for ch in value)
    )


def _apply_text_override(root: Path, override: TextOverride) -> None:
    rel = _safe_rel(override.path)
    path = root.joinpath(*rel.split("/"))
    if path.is_symlink() or not path.is_file():
        raise AssemblyError(f"override target is not a regular file: {rel}")
    if not _valid_sha256(override.before_sha256) or not _valid_sha256(override.after_sha256):
        raise AssemblyError(f"override digest is invalid: {rel}")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise AssemblyError(f"cannot read override target: {rel}") from exc
    if len(payload) > 1024 * 1024 or hashlib.sha256(payload).hexdigest() != override.before_sha256:
        raise AssemblyError(f"override input does not match reviewed bytes: {rel}")
    old = override.old.encode("utf-8")
    new = override.new.encode("utf-8")
    if not old or payload.count(old) != 1:
        raise AssemblyError(f"override match is not unique: {rel}")
    updated = payload.replace(old, new, 1)
    if hashlib.sha256(updated).hexdigest() != override.after_sha256:
        raise AssemblyError(f"override output does not match reviewed bytes: {rel}")
    mode = path.stat(follow_symlinks=False).st_mode & 0o777
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(updated); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        dirfd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try: os.fsync(dirfd)
        finally: os.close(dirfd)
    except BaseException:
        try: os.unlink(temporary)
        except FileNotFoundError: pass
        raise


def _fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _fsync_tree(root: Path) -> None:
    # sync(2) provides one filesystem-wide durability barrier without forcing
    # one journal transaction per member (large modpacks contain 10k+ files).
    if not root.is_dir() or root.is_symlink():
        raise AssemblyError("durability root is unsafe")
    os.sync()


def _safe_rel(raw: str) -> str:
    if not isinstance(raw, str) or not raw or "\\" in raw:
        raise AssemblyError(f"unsafe path: {raw!r}")
    p = PurePosixPath(raw)
    if p.is_absolute() or any(part in {"", ".", ".."} for part in p.parts):
        raise AssemblyError(f"unsafe path: {raw!r}")
    return "/".join(p.parts)


def _under(path: str, roots: Sequence[str]) -> bool:
    return any(path == root or path.startswith(root + "/") for root in roots)


def _zip_mode(info: zipfile.ZipInfo) -> int:
    return (info.external_attr >> 16) & 0o177777


def _validate_zip(info: zipfile.ZipInfo, seen: set[str], folded: set[str]) -> str:
    name = info.filename
    if info.is_dir():
        if not name.endswith("/") or name.endswith("//"):
            raise AssemblyError(f"unsafe archive directory: {name!r}")
        name = name[:-1]
    if not name:
        return ""
    path = _safe_rel(name)
    key = path.casefold()
    if path in seen:
        raise AssemblyError(f"duplicate archive member: {path}")
    if key in folded:
        raise AssemblyError(f"casefold collision: {path}")
    seen.add(path); folded.add(key)
    mode = _zip_mode(info)
    kind = mode & 0o170000
    if kind and kind != 0o100000 and kind != 0o040000:
        raise AssemblyError(f"special archive member: {path}")
    # ZipInfo has no portable hardlink bit; Unix mode and link metadata both
    # identify the common hardlink/symlink encodings.
    if (mode & 0o170000) == 0o120000 or info.create_system == 3 and (mode & 0o170000) == 0o060000:
        raise AssemblyError(f"link archive member: {path}")
    return path


def _normalize_spec(spec: AssemblySpec | Mapping[str, Any]) -> AssemblySpec:
    if isinstance(spec, AssemblySpec):
        return spec
    data = dict(spec)
    overlays = tuple(o if isinstance(o, Overlay) else Overlay(Path(o["source"]), o["destination"], o["sha256"]) for o in data.get("overlays", ()))
    return AssemblySpec(archive=Path(data["archive"]), archive_sha256=str(data["archive_sha256"]),
        archive_size=int(data["archive_size"]), members=data["members"],
        vendor_roots=tuple(_safe_rel(x).rstrip("/") for x in data.get("vendor_roots", ())),
        persistent_dirs=tuple(_safe_rel(x).rstrip("/") for x in data.get("persistent_dirs", ())),
        persistent_files=tuple(_safe_rel(x) for x in data.get("persistent_files", ())),
        required_persistent=tuple(_safe_rel(x) for x in data.get("required_persistent", ())), overlays=overlays,
        max_entries=int(data.get("max_entries", MAX_ARCHIVE_ENTRIES)), max_member_size=int(data.get("max_member_size", MAX_MEMBER_SIZE)),
        max_total_size=int(data.get("max_total_size", MAX_TOTAL_SIZE)), max_compression_ratio=int(data.get("max_compression_ratio", MAX_COMPRESSION_RATIO)))


def _copy_persistent(src: Path, dst: Path) -> None:
    """Copy state without following any symlink or special file."""
    if src.is_symlink():
        raise AssemblyError(f"persistent state contains a symlink: {src.name}")
    if src.is_file():
        shutil.copy2(src, dst)
        return
    if not src.is_dir():
        raise AssemblyError(f"persistent state is not regular: {src.name}")
    dst.mkdir(parents=True, exist_ok=False)
    for child in sorted(src.iterdir(), key=lambda p: p.name.casefold()):
        _copy_persistent(child, dst / child.name)


def assemble(spec: AssemblySpec | Mapping[str, Any], prior_runtime: Path, release: Path) -> AssemblyReport:
    """Verify, extract, overlay, and return a deterministic unactivated release."""
    cfg = _normalize_spec(spec)
    archive = cfg.archive
    if not archive.is_file():
        raise AssemblyError("pinned archive is missing")
    size, digest = _digest(archive)
    if cfg.archive_size < 0 or not isinstance(cfg.archive_sha256, str) or len(cfg.archive_sha256) != 64 or cfg.archive_sha256 != cfg.archive_sha256.lower() or any(c not in "0123456789abcdef" for c in cfg.archive_sha256):
        raise AssemblyError("invalid archive pin")
    if size != cfg.archive_size or digest != cfg.archive_sha256:
        raise AssemblyError("archive size or SHA256 mismatch")
    pins: dict[str, FilePin] = {}
    pin_folded: set[str] = set()
    for raw_path, value in cfg.members.items():
        normalized = _safe_rel(raw_path)
        if normalized in pins or normalized.casefold() in pin_folded:
            raise AssemblyError(f"manifest path collision: {normalized}")
        pins[normalized] = FilePin.from_value(normalized, value)
        pin_folded.add(normalized.casefold())
    roots = tuple(cfg.vendor_roots)
    if not roots:
        raise AssemblyError("vendor roots allowlist is required")
    release = Path(release); prior_runtime = Path(prior_runtime)
    if any(x <= 0 for x in (cfg.max_entries, cfg.max_member_size, cfg.max_total_size, cfg.max_compression_ratio)):
        raise AssemblyError("invalid archive bounds")
    if archive.resolve() in {prior_runtime.resolve(), release.resolve()} or prior_runtime.resolve() == release.resolve():
        raise AssemblyError("archive, prior runtime, and release must not alias")
    all_persistent = tuple(cfg.persistent_dirs) + tuple(cfg.persistent_files)
    if any(rel not in all_persistent for rel in cfg.required_persistent):
        raise AssemblyError("required persistent state must be explicitly declared")
    if len({p.casefold() for p in all_persistent}) != len(all_persistent) or any(a == b or a.startswith(b + "/") or b.startswith(a + "/") for i, a in enumerate(all_persistent) for b in all_persistent[i + 1:]):
        raise AssemblyError("persistent destinations overlap or duplicate")
    overlay_dests = tuple(_safe_rel(o.destination) for o in cfg.overlays)
    if len({x.casefold() for x in overlay_dests}) != len(overlay_dests):
        raise AssemblyError("overlay destinations duplicate")
    for overlay in cfg.overlays:
        if Path(overlay.source).resolve() in {archive.resolve(), prior_runtime.resolve(), release.resolve()}:
            raise AssemblyError("overlay source aliases assembly input")
    if release.exists():
        raise AssemblyError("release destination already exists")
    release.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".curated-", dir=release.parent))
    try:
        seen: set[str] = set(); folded: set[str] = set(); entries: dict[str, zipfile.ZipInfo] = {}
        with zipfile.ZipFile(archive) as zf:
            infos = zf.infolist()
            if len(infos) > cfg.max_entries:
                raise AssemblyError("archive entry count exceeds bound")
            total = 0
            for info in infos:
                path = _validate_zip(info, seen, folded)
                if not path: continue
                if info.flag_bits & 1:
                    raise AssemblyError(f"encrypted archive member: {path}")
                # Directory records are structural ZIP metadata; the exact
                # content manifest covers regular files only.
                if info.is_dir():
                    continue
                if info.file_size < 0 or info.file_size > cfg.max_member_size or info.file_size > MAX_MEMBER_SIZE:
                    raise AssemblyError(f"member size exceeds bound: {path}")
                if info.file_size and (info.compress_size <= 0 or info.file_size > info.compress_size * cfg.max_compression_ratio):
                    raise AssemblyError(f"member compression ratio exceeds bound: {path}")
                total += info.file_size
                if total > cfg.max_total_size:
                    raise AssemblyError("archive total size exceeds bound")
                entries[path] = info
                if path not in pins or not _under(path, roots):
                    raise AssemblyError(f"archive member is not exactly approved: {path}")
                pin = pins[path]
                dest = staging.joinpath(*path.split("/")); dest.parent.mkdir(parents=True, exist_ok=True)
                h = hashlib.sha256(); count = 0
                with zf.open(info, "r") as source, dest.open("wb") as target:
                    while chunk := source.read(1024 * 1024):
                        count += len(chunk); h.update(chunk); target.write(chunk)
                        if count > cfg.max_member_size: raise AssemblyError(f"member size exceeds bound: {path}")
                if count != pin.size or h.hexdigest() != pin.sha256:
                    raise AssemblyError(f"member checksum or size mismatch: {path}")
        if set(entries) != set(pins):
            raise AssemblyError("archive member set does not exactly match manifest")
        for rel in all_persistent:
            dst = staging.joinpath(*rel.split("/"))
            if any(p == dst or dst in p.parents for p in staging.rglob("*")):
                raise AssemblyError(f"persistent destination collides with vendor content: {rel}")
        for rel in overlay_dests:
            dst = staging.joinpath(*rel.split("/"))
            if any(p == dst or dst in p.parents for p in staging.rglob("*")):
                raise AssemblyError(f"overlay destination collides with vendor content: {rel}")
        for rel in cfg.required_persistent:
            required = prior_runtime.joinpath(*rel.split("/"))
            if not required.exists() or required.is_symlink():
                raise AssemblyError(f"required persistent state missing: {rel}")
        preserved: list[str] = []
        for rel in tuple(cfg.persistent_dirs) + tuple(cfg.persistent_files):
            src = prior_runtime.joinpath(*rel.split("/"))
            if not src.exists() or src.is_symlink():
                if rel in cfg.required_persistent: raise AssemblyError(f"required persistent state missing: {rel}")
                continue
            if rel in cfg.persistent_files and not src.is_file(): raise AssemblyError(f"persistent file is not regular: {rel}")
            if rel in cfg.persistent_dirs and not src.is_dir(): raise AssemblyError(f"persistent directory is not directory: {rel}")
            dst = staging.joinpath(*rel.split("/")); dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists() or any(p == dst or dst in p.parents for p in staging.rglob("*")) or any(p.is_file() for p in dst.parents if p != staging):
                raise AssemblyError(f"persistent destination collides with vendor content: {rel}")
            _copy_persistent(src, dst)
            preserved.append(rel)
        overlay_names: list[str] = []
        for overlay in cfg.overlays:
            dest_rel = _safe_rel(overlay.destination); src = Path(overlay.source)
            if src.is_symlink() or not src.is_file() or not isinstance(overlay.sha256, str) or len(overlay.sha256) != 64 or overlay.sha256 != overlay.sha256.lower() or _digest(src)[1] != overlay.sha256: raise AssemblyError(f"overlay hash failure: {dest_rel}")
            dst = staging.joinpath(*dest_rel.split("/"))
            if dst.exists() or any(p == dst or dst in p.parents for p in staging.rglob("*")) or any(p.is_file() for p in dst.parents if p != staging):
                raise AssemblyError(f"overlay destination collides with existing content: {dest_rel}")
            dst.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(src, dst)
            if _digest(dst)[1] != overlay.sha256:
                raise AssemblyError(f"overlay destination verification failed: {dest_rel}")
            overlay_names.append(dest_rel)
        if _digest(archive) != (cfg.archive_size, cfg.archive_sha256):
            raise AssemblyError("archive changed during assembly")
        # One complete durability pass immediately before the atomic publish
        # is sufficient. Per-member fsyncs made large modpacks spend minutes
        # forcing thousands of redundant journal commits.
        _fsync_tree(staging)
        fd = os.open(release.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try: os.fsync(fd)
        finally: os.close(fd)
        os.replace(staging, release)
        fd = os.open(release.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try: os.fsync(fd)
        finally: os.close(fd)
        files = tuple(sorted(p.relative_to(release).as_posix() for p in release.rglob("*") if p.is_file()))
        plan = tuple([f"vendor:{p}" for p in sorted(files)] + [f"preserve:{p}" for p in sorted(preserved)] + [f"overlay:{p}" for p in sorted(overlay_names)])
        return AssemblyReport(release, files, tuple(sorted(preserved)), tuple(sorted(overlay_names)), plan)
    except BaseException as exc:
        shutil.rmtree(staging, ignore_errors=True)
        if isinstance(exc, (zipfile.BadZipFile, zipfile.LargeZipFile)):
            raise AssemblyError("invalid ZIP archive") from exc
        raise


def assemble_versioned_runtime(
    vendor_release: Path,
    state_root: Path,
    version: str,
    prior_runtime: Path,
    release: Path,
    persistent_dirs: Sequence[str] = (),
    persistent_files: Sequence[str] = (),
    required_paths: Sequence[str] = (),
    mutable_vendor_dirs: Sequence[str] = ("config",),
    empty_mutable_dirs: Sequence[str] = (),
    fixed_symlinks: Mapping[str, Path] | None = None,
    text_overrides: Sequence[TextOverride] = (),
) -> AssemblyReport:
    """Build a release plus stable/versioned state links, without activation."""
    vendor_release = Path(vendor_release); state_root = Path(state_root)
    prior_runtime = Path(prior_runtime); release = Path(release)
    version = _safe_rel(version)
    if "/" in version:
        raise AssemblyError("version must be one path component")
    if not vendor_release.is_dir() or vendor_release.is_symlink():
        raise AssemblyError("verified vendor release must be a real directory")
    if release.exists() or release.is_symlink() or state_root.is_symlink():
        raise AssemblyError("release and state roots must be new non-symlink roots")
    for parent in (state_root.parent, release.parent):
        if not parent.is_dir() or parent.is_symlink():
            raise AssemblyError("runtime parent must be a real directory")
    roots = [vendor_release.resolve(), state_root.resolve(), release.resolve(), prior_runtime.resolve()]
    if len(set(roots)) != len(roots) or any(a == b or str(a).startswith(str(b) + os.sep) or str(b).startswith(str(a) + os.sep) for i, a in enumerate(roots) for b in roots[i + 1:]):
        raise AssemblyError("runtime roots alias or overlap")
    dirs = tuple(_safe_rel(x) for x in persistent_dirs)
    files = tuple(_safe_rel(x) for x in persistent_files)
    required = tuple(_safe_rel(x) for x in required_paths)
    mutable = tuple(_safe_rel(x) for x in mutable_vendor_dirs)
    empty_mutable = tuple(_safe_rel(x) for x in empty_mutable_dirs)
    fixed = {_safe_rel(key): Path(value) for key, value in (fixed_symlinks or {}).items()}
    overrides = tuple(text_overrides)
    declared = dirs + files
    if len({x.casefold() for x in declared}) != len(declared) or any(a == b or a.startswith(b + "/") or b.startswith(a + "/") for i, a in enumerate(declared) for b in declared[i + 1:]):
        raise AssemblyError("persistent paths overlap or duplicate")
    all_mutable = mutable + empty_mutable
    if any(x not in declared for x in required) or len({x.casefold() for x in all_mutable}) != len(all_mutable):
        raise AssemblyError("required or mutable paths are not explicitly declared")
    if any(a == b or a.startswith(b + "/") or b.startswith(a + "/") for i, a in enumerate(all_mutable) for b in all_mutable[i + 1:]):
        raise AssemblyError("mutable vendor paths overlap")
    if any(a == b or a.startswith(b + "/") or b.startswith(a + "/") for a in all_mutable for b in declared):
        raise AssemblyError("mutable vendor path overlaps persistent state")
    controlled = declared + all_mutable + tuple(fixed)
    if len({x.casefold() for x in controlled}) != len(controlled) or any(
        a == b or a.startswith(b + "/") or b.startswith(a + "/")
        for i, a in enumerate(controlled) for b in controlled[i + 1:]
    ):
        raise AssemblyError("controlled runtime paths overlap")
    for target in fixed.values():
        if not target.is_absolute() or target.is_symlink() or not target.exists():
            raise AssemblyError("fixed runtime symlink target is unsafe")
    override_paths = tuple(_safe_rel(item.path) for item in overrides)
    if len({path.casefold() for path in override_paths}) != len(override_paths):
        raise AssemblyError("text override paths duplicate")
    if any(not _under(path, all_mutable) for path in override_paths):
        raise AssemblyError("text overrides must target version-specific mutable state")
    state_created = False; release_published = False
    state_stage: Path | None = None; release_stage: Path | None = None; version_state: Path | None = None
    try:
        if state_root.exists():
            if not state_root.is_dir() or state_root.is_symlink(): raise AssemblyError("existing state root is unsafe")
            state = state_root
            for rel in required:
                p = state.joinpath(*rel.split("/"))
                if not p.exists() or p.is_symlink(): raise AssemblyError(f"required state missing: {rel}")
        else:
            state_stage = Path(tempfile.mkdtemp(prefix=".state-", dir=state_root.parent))
            state = state_stage
            for rel in declared:
                src = prior_runtime.joinpath(*rel.split("/"))
                if not src.exists() or src.is_symlink():
                    if rel in required: raise AssemblyError(f"required state missing: {rel}")
                    continue
                dst = state.joinpath(*rel.split("/")); dst.parent.mkdir(parents=True, exist_ok=True); _copy_persistent(src, dst)
            for rel in required:
                if not state.joinpath(*rel.split("/")).exists(): raise AssemblyError(f"required state missing: {rel}")
            _fsync_tree(state); os.replace(state_stage, state_root); state_stage = None; state_created = True; state = state_root
        versions_root = state.joinpath(".versions")
        if versions_root.is_symlink() or (versions_root.exists() and not versions_root.is_dir()):
            raise AssemblyError("version-state root is unsafe")
        version_state = versions_root / version
        if version_state.exists() or version_state.is_symlink(): raise AssemblyError("version state already exists")
        version_state.mkdir(parents=True)
        release_stage = Path(tempfile.mkdtemp(prefix=".release-", dir=release.parent))
        release_stage.rmdir()
        _copy_persistent(vendor_release, release_stage)
        for rel in mutable:
            source = release_stage.joinpath(*rel.split("/"))
            if not source.is_dir() or source.is_symlink(): raise AssemblyError(f"mutable vendor directory missing: {rel}")
            target = version_state.joinpath(*rel.split("/")); target.parent.mkdir(parents=True, exist_ok=True); _copy_persistent(source, target); shutil.rmtree(source)
            os.symlink(str(target), source)
        for rel in empty_mutable:
            source = release_stage.joinpath(*rel.split("/"))
            if source.exists() or source.is_symlink():
                raise AssemblyError(f"empty mutable directory collides with vendor content: {rel}")
            target = version_state.joinpath(*rel.split("/")); target.mkdir(parents=True, exist_ok=False)
            source.parent.mkdir(parents=True, exist_ok=True); os.symlink(str(target), source)
        for override in overrides:
            _apply_text_override(version_state, override)
        for rel in declared:
            link = release_stage.joinpath(*rel.split("/")); target = state_root.joinpath(*rel.split("/"))
            if link.is_symlink():
                raise AssemblyError(f"persistent release collision: {rel}")
            if link.exists():
                if rel in files and not link.is_file():
                    raise AssemblyError(f"persistent vendor type mismatch: {rel}")
                if rel in dirs and not link.is_dir():
                    raise AssemblyError(f"persistent vendor type mismatch: {rel}")
                if link.is_dir():
                    shutil.rmtree(link)
                else:
                    link.unlink()
            link.parent.mkdir(parents=True, exist_ok=True); os.symlink(str(target), link)
        for rel, target in fixed.items():
            link = release_stage.joinpath(*rel.split("/"))
            if link.exists() or link.is_symlink():
                raise AssemblyError(f"fixed runtime symlink collision: {rel}")
            link.parent.mkdir(parents=True, exist_ok=True); os.symlink(str(target), link)
        _fsync_tree(release_stage); _fsync_tree(version_state)
        os.replace(release_stage, release); release_stage = None; release_published = True
        for parent in (state_root, state_root.parent, versions_root, release.parent):
            fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try: os.fsync(fd)
            finally: os.close(fd)
        files_out = tuple(sorted(p.relative_to(release).as_posix() for p in release.rglob("*") if p.is_file() or p.is_symlink()))
        overlay_paths = tuple(sorted(all_mutable + tuple(fixed)))
        plan = tuple(
            [f"preserve:{x}" for x in sorted(declared)]
            + [f"mutable:{x}" for x in sorted(all_mutable)]
            + [f"fixed-link:{x}" for x in sorted(fixed)]
        )
        return AssemblyReport(release, files_out, tuple(sorted(declared)), overlay_paths, plan)
    except BaseException:
        if release_stage is not None: shutil.rmtree(release_stage, ignore_errors=True)
        if release_published: shutil.rmtree(release, ignore_errors=True)
        if state_stage is not None: shutil.rmtree(state_stage, ignore_errors=True)
        if version_state is not None and not state_created: shutil.rmtree(version_state, ignore_errors=True)
        if state_created: shutil.rmtree(state_root, ignore_errors=True)
        raise


assemble_curated_release = assemble
