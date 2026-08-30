from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from tools.quality import check_public_boundary as boundary


ROOT = Path(__file__).resolve().parents[1]


def _repository(tmp_path: Path, files: dict[str, str | bytes]) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    return root


def _categories(root: Path) -> set[tuple[str, int, str]]:
    return {(item.path, item.line, item.category) for item in boundary.scan_repository(root)}


def test_clean_tracked_reference_uses_documentation_networks_and_example_namespaces(tmp_path: Path) -> None:
    root = _repository(
        tmp_path,
        {
            "README.md": "https://github.com/swagsystems/helios-horizon\nhttp://127.0.0.1:8444\n",
            "docs/reference.md": "mc.example.com 192.0.2.10 198.51.100.20 203.0.113.30\n",
            "config/examples/profile.toml": (
                'config = "/etc/horizon-example/profiles.d/server.toml"\n'
                'state = "/var/lib/horizon-example/state.db"\n'
                'run = "/run/horizon-example/control.sock"\n'
                'data = "/srv/example-minecraft/world"\n'
                'release = "/opt/example-minecraft/releases/v1"\n'
                'backup = "/var/backups/example-minecraft"\n'
                'helper = "/usr/local/libexec/horizon-example-wake"\n'
            ),
        },
    )

    assert boundary.scan_repository(root) == ()


@pytest.mark.parametrize(
    ("value", "category"),
    [
        (".".join(("games", "heliosorbit", "space")), "private-domain"),
        (".".join(("10", "1", "2", "3")), "private-address"),
        (".".join(("172", "31", "2", "3")), "private-address"),
        (".".join(("192", "168", "2", "3")), "private-address"),
        (".".join(("100", "100", "2", "3")), "private-address"),
        (".".join(("server", "internal")), "private-dns"),
    ],
)
def test_global_private_topology_is_refused_without_echoing_content(
    tmp_path: Path, value: str, category: str
) -> None:
    root = _repository(tmp_path, {"src/example.py": f'VALUE = "{value}"\n'})

    findings = boundary.scan_repository(root)

    assert findings == (boundary.Finding("src/example.py", 1, category),)
    assert value not in findings[0].render()


def test_example_absolute_path_must_use_the_explicit_namespace(tmp_path: Path) -> None:
    root = _repository(
        tmp_path,
        {"config/examples/profile.toml": 'state = "/var/lib/game-control/state.db"\n'},
    )

    assert _categories(root) == {("config/examples/profile.toml", 1, "example-path-namespace")}


def test_non_dns_programming_tokens_are_narrowly_exempt(tmp_path: Path) -> None:
    enum_token = "BackupDestination" + "." + "LOCAL"
    property_token = "user" + "." + "home"
    root = _repository(tmp_path, {"src/example.py": f"{enum_token}\n{property_token}\n"})

    assert boundary.scan_repository(root) == ()


def test_untracked_content_is_not_scanned(tmp_path: Path) -> None:
    root = _repository(tmp_path, {"README.md": "example.com\n"})
    private_address = ".".join(("10", "2", "3", "4"))
    (root / "untracked.txt").write_text(private_address, encoding="utf-8")

    assert boundary.scan_repository(root) == ()


def test_undecodable_tracked_text_fails_closed_without_content(tmp_path: Path) -> None:
    root = _repository(tmp_path, {"README.md": b"\xff\xfe\x00"})

    with pytest.raises(boundary.ScanError, match="tracked-text-unreadable:README.md"):
        boundary.scan_repository(root)


def test_known_binary_asset_is_skipped(tmp_path: Path) -> None:
    root = _repository(tmp_path, {"docs/diagram.png": b"\xff\xfe\x00"})

    assert boundary.scan_repository(root) == ()


def test_tracked_symlink_fails_closed(tmp_path: Path) -> None:
    root = _repository(tmp_path, {"target.txt": "safe\n"})
    link = root / "tracked-link"
    link.symlink_to("target.txt")
    subprocess.run(["git", "-C", str(root), "add", "tracked-link"], check=True)

    with pytest.raises(boundary.ScanError, match="tracked-file-not-regular:tracked-link"):
        boundary.scan_repository(root)


def test_diagnostic_unsafe_tracked_path_fails_closed(tmp_path: Path) -> None:
    root = _repository(tmp_path, {"unsafe:name.txt": "safe\n"})

    with pytest.raises(boundary.ScanError, match="unsafe-tracked-path"):
        boundary.scan_repository(root)


def test_policy_validation_rejects_malformed_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(boundary, "NON_DNS_TOKENS", frozenset({"bad\nentry"}))

    with pytest.raises(boundary.ScanError, match="invalid-dns-allowlist"):
        boundary._validate_policy()


def test_cli_requires_an_explicit_repository_root() -> None:
    with pytest.raises(SystemExit) as exc_info:
        boundary.main([])

    assert exc_info.value.code == 2


def test_cli_diagnostics_contain_only_path_line_and_category(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    private_address = ".".join(("10", "2", "3", "4"))
    root = _repository(tmp_path, {"README.md": f"endpoint={private_address}\n"})

    assert boundary.main(["--root", str(root)]) == 1

    captured = capsys.readouterr()
    assert captured.out == "README.md:1:private-address\n"
    assert private_address not in captured.out
    assert captured.err == ""


def test_quality_gates_invoke_the_scanner() -> None:
    command = "tools/quality/check_public_boundary.py --root"

    assert command in (ROOT / "scripts/check.sh").read_text(encoding="utf-8")
    assert command in (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
