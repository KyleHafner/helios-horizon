"""Typed, fail-closed editing of the small profile-owned config surface."""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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


def set_profile_config(profile: Any, changes: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(changes, dict) or not changes:
        raise ConfigValidationError("changes must not be empty")
    settings = {setting.key: setting for setting in _settings(profile)}
    unknown = set(changes) - set(settings)
    if unknown:
        raise ConfigValidationError("unknown config key")
    path = _path(profile)
    lines, current = _read(path)
    encoded = {key: _validate(settings[key], value) for key, value in changes.items()}
    changed = sorted(key for key, value in encoded.items() if current.get(key) != value)
    if not changed:
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
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.{stamp}.bak")
    shutil.copy2(path, backup)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.writelines(output)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
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


__all__ = ["ConfigValidationError", "WHITELIST", "get_profile_config", "set_profile_config"]
