from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from game_control.deployment_manifest import (
    DeploymentManifest,
    FileSpec,
    get_manifest,
    manifest_digest,
)


def test_manifest_is_typed_frozen_and_exactly_sized() -> None:
    manifest = get_manifest()
    assert isinstance(manifest, DeploymentManifest)
    assert len(manifest.files) == 65
    assert len(manifest.directories) == 48
    assert len(manifest.runtime_sources) == 69
    assert len(manifest.runtime_files_for()) == 138
    assert all(isinstance(value, tuple) for value in (manifest.files, manifest.directories, manifest.runtime_sources))
    with pytest.raises(FrozenInstanceError):
        manifest.schema_version = 2  # type: ignore[misc]


def test_manifest_projections_remap_only_targets() -> None:
    manifest = get_manifest()
    projected = manifest.files_for(Path("/stage"))
    assert projected[0].target.startswith("/stage/")
    assert projected[0].source == manifest.files[0].source
    assert manifest.links_for(Path("/stage"))[0].target.startswith("/stage/")
    assert manifest.links_for(Path("/stage"))[0].link_target.startswith("/")


def test_manifest_validation_checks_real_sources_without_import_side_effects() -> None:
    get_manifest().validate(Path("."))
    assert len(manifest_digest()) == 64


@pytest.mark.parametrize("source", ["", "/absolute.py", "a/../b.py", "a\\b.py", "."])
def test_file_spec_rejects_unsafe_source(source: str) -> None:
    with pytest.raises(ValueError):
        FileSpec(source, "/opt/example", 0o644)
