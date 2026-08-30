from __future__ import annotations

from pathlib import Path


ADR = (
    Path(__file__).resolve().parents[1]
    / "docs/adr/0001-dynamic-server-instances-from-reviewed-templates.md"
)


def test_dynamic_server_adr_is_design_only_and_preserves_browser_authority() -> None:
    content = ADR.read_text(encoding="utf-8")
    normalized = " ".join(content.split())

    for model in ("GameKind", "ServerInstanceId", "TemplateId", "Blueprint", "CompiledProfile"):
        assert f"`{model}`" in content
    for forbidden_input in (
        "filesystem paths",
        "systemd units",
        "executables",
        "arbitrary JVM arguments",
        "shell scripts",
        "RCON endpoints",
        "credentials",
        "arbitrary URLs",
    ):
        assert forbidden_input in normalized
    assert "out of scope for Wave 7" in normalized
    assert "does not authorize or implement" in normalized


def test_dynamic_server_adr_covers_acquisition_publication_and_recovery() -> None:
    content = ADR.read_text(encoding="utf-8")
    normalized = " ".join(content.split())

    for provider in (
        "Modrinth",
        "CurseForge",
        "pasted links",
        "uploaded Modrinth packs",
        "generic prepared server archives",
        "existing-server import",
    ):
        assert provider in normalized
    for stage in (
        "Resolve unprivileged",
        "Download bounded artifacts",
        "content-addressed cache",
        "Verify identity",
        "Extract safely",
        "Apply reviewed loader setup",
        "Generate the runtime command",
        "Run a staged startup test",
        "Publish atomically",
        "Write lockfile and provenance",
    ):
        assert stage in normalized
    assert "Pack-provided scripts" in normalized
    assert "never run as root" in normalized
    assert "Rollback and recovery" in content
    assert "Threat model" in content
    assert "Migration constraints" in content
    assert "Deferred owner decisions" in content
