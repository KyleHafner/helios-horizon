from __future__ import annotations

from email.parser import BytesParser
from pathlib import Path
import subprocess
import tomllib
import zipfile


ROOT = Path(__file__).resolve().parents[1]
PROJECT_URL = "https://github.com/swagsystems/helios-horizon"


def _project() -> dict[str, object]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def test_public_project_metadata_is_complete_and_canonical() -> None:
    project = _project()
    identity = [{"name": "Swag Systems"}]

    assert project["description"] == "Security-focused single-slot control plane for self-hosted game servers"
    assert project["readme"] == "README.md"
    assert project["license"] == "MIT"
    assert project["authors"] == identity
    assert project["maintainers"] == identity
    assert project["urls"] == {
        "Homepage": PROJECT_URL,
        "Repository": PROJECT_URL,
        "Issues": f"{PROJECT_URL}/issues",
    }
    assert project["scripts"] == {"horizon": "game_control.cli:main"}
    classifiers = set(project["classifiers"])
    assert "Programming Language :: Python :: 3.11" in classifiers
    assert "Programming Language :: Python :: 3.12" in classifiers
    assert "License :: OSI Approved :: MIT License" in classifiers
    assert "httpx2" not in (ROOT / "pyproject.toml").read_text(encoding="utf-8").lower()
    assert "httpx2" not in (ROOT / "uv.lock").read_text(encoding="utf-8").lower()


def test_built_wheel_exposes_the_public_metadata(tmp_path: Path) -> None:
    output = tmp_path / "dist"
    subprocess.run(
        ["uv", "build", "--offline", "--wheel", "--out-dir", str(output)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(output.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        metadata_name = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
        metadata = BytesParser().parsebytes(archive.read(metadata_name))

    assert metadata["Summary"] == _project()["description"]
    assert metadata.get_all("Author") == ["Swag Systems"]
    assert metadata.get_all("Maintainer") == ["Swag Systems"]
    assert metadata["License-Expression"] == "MIT"
    assert set(metadata.get_all("Project-URL")) == {
        f"Homepage, {PROJECT_URL}",
        f"Repository, {PROJECT_URL}",
        f"Issues, {PROJECT_URL}/issues",
    }
    assert set(metadata.get_all("Classifier")) >= {
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    }
