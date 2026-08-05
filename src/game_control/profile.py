from __future__ import annotations

import os
import re
import tempfile
import tomllib
from pathlib import Path
from types import MappingProxyType
from typing import Iterator

from pydantic import ValidationError

from .models import AdapterKind, PathSpec, Profile, ProfileId


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validate_paths(paths: PathSpec) -> None:
    """Ensure profile paths cannot escape their declared roots through symlinks."""
    roots = tuple(paths.data_roots)
    for path in (paths.mutable_root, paths.install_root, paths.version_file, *paths.log_files):
        if not any(_within(path, root) for root in roots):
            raise ValueError("profile path is outside data root")
    for path in (*paths.data_roots, paths.mutable_root, paths.install_root, paths.version_file, *paths.log_files):
        resolved = path.resolve(strict=False)
        if not any(_within(resolved, root.resolve(strict=False)) for root in roots):
            raise ValueError("profile path escapes data root through symlink")
    backup_resolved = paths.backup_root.resolve(strict=False)
    resolved_roots = tuple(root.resolve(strict=False) for root in roots)
    if any(
        _within(paths.backup_root, root)
        or _within(root, paths.backup_root)
        or _within(backup_resolved, root_resolved)
        or _within(root_resolved, backup_resolved)
        for root, root_resolved in zip(roots, resolved_roots)
    ):
        raise ValueError("backup_root must be outside data roots")


class ProfileRegistry:
    def __init__(self, profiles: MappingProxyType[str, Profile]):
        self._profiles = profiles
        self._root: Path | None = None

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "ProfileRegistry":
        root = Path(path)
        if not root.is_dir():
            raise ValueError("profile directory does not exist")
        profiles: dict[str, Profile] = {}
        for file in sorted(root.glob("*.toml")):
            try:
                with file.open("rb") as stream:
                    raw = tomllib.load(stream)
                if raw.get("adapter") == AdapterKind.SYSTEMD.value:
                    unit = raw.get("systemd_unit")
                    if not isinstance(unit, str) or not re.fullmatch(
                        r"[a-z0-9@_.-]+\.service", unit
                    ):
                        raise ValueError("invalid systemd unit")
                profile = Profile.model_validate(raw)
            except ValueError:
                raise
            except (OSError, tomllib.TOMLDecodeError, ValidationError) as exc:
                raise ValueError(f"invalid profile {file.name}: {exc}") from exc
            if file.stem != profile.id.value:
                raise ValueError("profile filename stem must equal id")
            if profile.id.value in profiles:
                raise ValueError("duplicate profile id")
            if len({(spec.protocol, spec.port) for spec in profile.ports}) != len(profile.ports):
                raise ValueError("duplicate port")
            if profile.adapter is AdapterKind.SYSTEMD and (
                profile.systemd_unit is None or "/" in profile.systemd_unit or ".." in profile.systemd_unit
            ):
                raise ValueError("invalid systemd unit")
            _validate_paths(profile.paths)
            profiles[profile.id.value] = profile
        registry = cls(MappingProxyType(profiles))
        registry._root = root
        return registry

    def update_idle_stop(self, profile_id: str | ProfileId, minutes: int) -> Profile:
        """Persist a validated root-scope idle-stop value and refresh the profile."""
        if not isinstance(minutes, int) or minutes < 0 or minutes > 1440 or 0 < minutes < 5:
            raise ValueError("idle_stop_minutes must be 0 or between 5 and 1440 minutes")
        key = profile_id.value if isinstance(profile_id, ProfileId) else str(profile_id)
        current = self.require(key)
        if self._root is None:
            raise RuntimeError("profile registry is not writable")
        path = self._root / f"{key}.toml"
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("profile configuration is unavailable")
        raw = path.read_text(encoding="utf-8")
        line = f"idle_stop_minutes = {minutes}"
        if re.search(r"(?m)^idle_stop_minutes\s*=\s*\d+\s*$", raw):
            raw = re.sub(r"(?m)^idle_stop_minutes\s*=\s*\d+\s*$", line, raw, count=1)
        else:
            marker = re.search(r"(?m)^\[", raw)
            insert_at = marker.start() if marker else len(raw)
            prefix = raw[:insert_at]
            if prefix and not prefix.endswith("\n"):
                prefix += "\n"
            raw = f"{prefix}{line}\n{raw[insert_at:]}"
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary_path = Path(temporary)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary_path, path.stat().st_mode & 0o777)
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)
        updated = current.model_copy(update={"idle_stop_minutes": minutes})
        self._profiles = MappingProxyType({**self._profiles, key: updated})
        return updated

    def require(self, profile_id: str | ProfileId) -> Profile:
        key = profile_id.value if isinstance(profile_id, ProfileId) else profile_id
        return self._profiles[key]

    def __iter__(self) -> Iterator[Profile]:
        return iter(self._profiles.values())

    def __len__(self) -> int:
        return len(self._profiles)

    @property
    def profiles(self) -> tuple[Profile, ...]:
        return tuple(self._profiles.values())
