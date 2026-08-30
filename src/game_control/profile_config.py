"""Typed, fail-closed editing of the small profile-owned config surface."""

from __future__ import annotations

import os
import stat
import hashlib
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


class ConfigValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Setting:
    key: str
    type: str
    restart_required: bool = True
    minimum: int | None = None
    maximum: int | None = None
    maximum_length: int | None = None
    choices: tuple[str, ...] = ()
    secret: bool = False

    def bounds(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if self.minimum is not None:
            result["min"] = self.minimum
        if self.maximum is not None:
            result["max"] = self.maximum
        if self.maximum_length is not None:
            result["max_length"] = self.maximum_length
        if self.choices:
            result["choices"] = list(self.choices)
        return result


WHITELIST: dict[str, tuple[Setting, ...]] = {
    "minecraft": (
        Setting("motd", "str", maximum_length=59), Setting("max-players", "int", minimum=1, maximum=64),
        Setting("view-distance", "int", minimum=4, maximum=16), Setting("difficulty", "enum", choices=("peaceful", "easy", "normal", "hard")),
        Setting("pvp", "bool"), Setting("white-list", "bool"),
    ),
    "minecraft-sunlit-cobblemon": (
        Setting("motd", "str", maximum_length=59), Setting("max-players", "int", minimum=1, maximum=64),
        Setting("view-distance", "int", minimum=4, maximum=16), Setting("difficulty", "enum", choices=("peaceful", "easy", "normal", "hard")),
        Setting("pvp", "bool"), Setting("white-list", "bool"),
    ),
    "terraria-vanilla": (
        Setting("maxplayers", "int", minimum=1, maximum=16), Setting("motd", "str", maximum_length=120),
        Setting("password", "str", maximum_length=120, secret=True), Setting("secure", "bool"),
    ),
    "terraria-tmod": (
        Setting("maxplayers", "int", minimum=1, maximum=16), Setting("motd", "str", maximum_length=120),
        Setting("password", "str", maximum_length=120, secret=True), Setting("secure", "bool"),
    ),
    "pz-rising": (
        Setting("PublicName", "str", maximum_length=64), Setting("MaxPlayers", "int", minimum=1, maximum=32),
        Setting("PauseEmpty", "bool"), Setting("PVP", "bool"),
    ),
}
_CONFIG_HISTORY_BASE = Path("/var/lib/game-control/config-history")


def _id(profile: Any) -> str:
    return str(getattr(getattr(profile, "id", ""), "value", getattr(profile, "id", "")))


def _path(profile: Any) -> Path:
    root = Path(profile.paths.mutable_root)
    profile_id = _id(profile)
    relative = {
        "minecraft": "server.properties",
        "minecraft-sunlit-cobblemon": "server.properties",
        "terraria-vanilla": "config/serverconfig.txt",
        "terraria-tmod": "config/serverconfig.txt",
        "pz-rising": "Server/servertest.ini",
    }.get(profile_id)
    if relative is None:
        raise ConfigValidationError("profile config is not supported")
    path = root / relative
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    if resolved_root not in resolved_path.parents or path.is_symlink() or path.parent.is_symlink():
        raise ConfigValidationError("profile config path is outside the profile root")
    return path


def _settings(profile: Any) -> tuple[Setting, ...]:
    try:
        return WHITELIST[_id(profile)]
    except KeyError as exc:
        raise ConfigValidationError("profile config is not supported") from exc


def _read(path: Path) -> tuple[list[str], dict[str, str]]:
    if not path.is_file() or path.is_symlink():
        raise ConfigValidationError("profile config file is unavailable")
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    values: dict[str, str] = {}
    for line in lines:
        if "=" not in line or line.lstrip().startswith(("#", ";")):
            continue
        key, value = line.rstrip("\r\n").split("=", 1)
        values[key.strip()] = value
    return lines, values


def _validate(setting: Setting, value: Any) -> str:
    if setting.type == "bool":
        if not isinstance(value, bool):
            raise ConfigValidationError(f"{setting.key} must be a boolean")
        return "true" if value else "false"
    if setting.type == "int":
        if isinstance(value, bool) or not isinstance(value, int) or not setting.minimum <= value <= setting.maximum:  # type: ignore[operator]
            raise ConfigValidationError(f"{setting.key} is outside its allowed range")
        return str(value)
    if not isinstance(value, str) or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ConfigValidationError(f"{setting.key} contains a control character")
    if setting.maximum_length is not None and len(value) > setting.maximum_length:
        raise ConfigValidationError(f"{setting.key} is too long")
    if setting.choices and value not in setting.choices:
        raise ConfigValidationError(f"{setting.key} is not an allowed value")
    return value


def get_profile_config(profile: Any) -> list[dict[str, Any]]:
    _, values = _read(_path(profile))
    result = []
    for setting in _settings(profile):
        raw = values.get(setting.key)
        result.append({
            "key": setting.key,
            "value": None if setting.secret else _decode(setting, raw),
            "configured": raw is not None if setting.secret else None,
            "type": setting.type,
            "bounds": setting.bounds(),
            "restart_required": setting.restart_required,
        })
    return result


def set_profile_config(
    profile: Any,
    changes: dict[str, Any],
    *,
    lease_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    if not isinstance(changes, dict) or not changes:
        raise ConfigValidationError("changes must not be empty")
    settings = {setting.key: setting for setting in _settings(profile)}
    unknown = set(changes) - set(settings)
    if unknown:
        raise ConfigValidationError("unknown config key")
    path = _path(profile)
    # Do not put history below a game-owned directory.  The configured parent
    # is checked before use and the history directory is root-owned, private,
    # and created outside the mutable tree.
    root = Path(profile.paths.mutable_root)
    history_parent = Path(getattr(profile.paths, "config_history_root", _CONFIG_HISTORY_BASE))
    history_root = history_parent / _id(profile)
    _ensure_root_owned_dir(history_root)
    encoded = {key: _validate(settings[key], value) for key, value in changes.items()}
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    relative = path.relative_to(root)
    try:
        lines, current, original, source_fd = _secure_read_fd(root_fd, relative)
    except Exception:
        os.close(root_fd)
        raise
    changed = sorted(key for key, value in encoded.items() if current.get(key) != value)
    if not changed:
        os.close(source_fd)
        os.close(root_fd)
        return {"changed": [], "restart_required": []}
    output: list[str] = []
    found: set[str] = set()
    for line in lines:
        if "=" in line and not line.lstrip().startswith(("#", ";")):
            key = line.rstrip("\r\n").split("=", 1)[0].strip()
            if key in encoded:
                output.append(f"{key}={encoded[key]}\n")
                found.add(key)
                continue
        output.append(line)
    for key in sorted(set(encoded) - found):
        output.append(f"{key}={encoded[key]}\n")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ%f")
    backup_name = f"{path.name}.{stamp}.bak"
    try:
        source_info = os.fstat(source_fd)
        if (source_info.st_dev, source_info.st_ino, source_info.st_size, source_info.st_mtime_ns, source_info.st_uid, source_info.st_gid, source_info.st_mode) != original[:7]:
            raise ConfigValidationError("profile config changed during edit")
        os.lseek(source_fd, 0, os.SEEK_SET)
        if hashlib.sha256(os.read(source_fd, source_info.st_size)).hexdigest() != original[7]:
            raise ConfigValidationError("profile config changed during edit")
        history_fd = os.open(history_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            backup_fd = os.open(backup_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=history_fd)
            try:
                os.lseek(source_fd, 0, os.SEEK_SET)
                with os.fdopen(source_fd, "rb", closefd=False) as source, os.fdopen(backup_fd, "wb", closefd=False) as destination:
                    shutil.copyfileobj(source, destination)
                    destination.flush()
                    os.fsync(destination.fileno())
            finally:
                os.close(backup_fd)
        finally:
            os.close(history_fd)
            os.close(source_fd)
    except OSError as exc:
        raise ConfigValidationError("profile config history is unavailable") from exc

    config_parent_fd = _open_beneath_dir_fd(root_fd, relative.parent)
    temp_name = f".{path.name}.{os.getpid()}.{uuid4_hex()}"
    fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640, dir_fd=config_parent_fd)
    try:
        os.fchmod(fd, stat.S_IMODE(original[6]))
        os.fchown(fd, original[4], original[5])
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.writelines(output)
            stream.flush()
            os.fsync(stream.fileno())
        current_info = os.stat(path.name, dir_fd=config_parent_fd, follow_symlinks=False)
        if (current_info.st_dev, current_info.st_ino) != (original[0], original[1]):
            raise ConfigValidationError("profile config changed during edit")
        if lease_check is not None and not lease_check():
            raise RuntimeError("operation lease was lost before config publication")
        os.rename(temp_name, path.name, src_dir_fd=config_parent_fd, dst_dir_fd=config_parent_fd)
        os.fsync(config_parent_fd)
    finally:
        try:
            os.unlink(temp_name, dir_fd=config_parent_fd)
        except FileNotFoundError:
            pass
        os.close(config_parent_fd)
        os.close(root_fd)
    restart = sorted(key for key in changed if settings[key].restart_required)
    return {"changed": changed, "restart_required": restart}


def _decode(setting: Setting, value: str | None) -> Any:
    if value is None:
        return None
    if setting.type == "bool":
        return value.lower() == "true"
    if setting.type == "int":
        try:
            return int(value)
        except ValueError:
            return None
    return value


def uuid4_hex() -> str:
    return os.urandom(16).hex()


def _ensure_root_owned_dir(path: Path) -> None:
    for ancestor in path.parents:
        if not ancestor.exists():
            continue
        info = os.lstat(ancestor)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_gid != 0:
            raise ConfigValidationError("profile config history is not trusted")
        if ancestor == Path("/"):
            break
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_gid != 0:
        raise ConfigValidationError("profile config history is not trusted")
    os.chmod(path, 0o700)


def _open_beneath_dir_fd(fd: int, relative: Path) -> int:
    fd = os.dup(fd)
    try:
        for component in relative.parts:
            if component in ("", "."):
                continue
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_beneath_fd(root_fd: int, relative: Path, flags: int) -> int:
    parent = _open_beneath_dir_fd(root_fd, relative.parent)
    try:
        return os.open(relative.name, flags | os.O_NOFOLLOW, dir_fd=parent)
    finally:
        os.close(parent)


def _secure_read_fd(root_fd: int, relative: Path) -> tuple[list[str], dict[str, str], tuple[int, int, int, int, int, int, int, str], int]:
    try:
        fd = _open_beneath_fd(root_fd, relative, os.O_RDONLY)
        info = os.fstat(fd)
        with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as stream:
            lines, values = _parse_lines(stream.read().splitlines(keepends=True))
        digest = hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()
        return lines, values, (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_uid, info.st_gid, info.st_mode, digest), fd
    except (OSError, UnicodeError) as exc:
        raise ConfigValidationError("profile config file is unavailable") from exc


def _parse_lines(lines: list[str]) -> tuple[list[str], dict[str, str]]:
    values: dict[str, str] = {}
    for line in lines:
        if "=" not in line or line.lstrip().startswith(("#", ";")):
            continue
        key, value = line.rstrip("\r\n").split("=", 1)
        values[key.strip()] = value
    return lines, values


__all__ = ["ConfigValidationError", "WHITELIST", "get_profile_config", "set_profile_config"]
