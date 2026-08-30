from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_readme_declares_the_three_repository_boundaries() -> None:
    readme = _read("README.md")

    assert "Reusable product" in readme
    assert "Sanitized reference deployment" in readme
    assert "External private deployment overlay" in readme
    assert "not safe to install unchanged" in readme
    assert "live console endpoint" in readme
    assert "Horizon VM" not in readme
    assert "three retained" not in readme
    assert "live-operation" not in readme
    for target in (
        "docs/architecture.md",
        "docs/security-model.md",
        "docs/engineering/security-and-lifecycle-invariants.md",
        "docs/deployment-example.md",
        "docs/operations-example.md",
    ):
        assert target in readme


def test_deployment_guide_keeps_private_policy_external_and_source_examples_non_live() -> None:
    deployment = _read("docs/deployment-example.md")
    normalized = " ".join(deployment.split())

    assert "external private deployment overlay" in normalized
    assert "not a snapshot of any current host" in normalized
    assert "not safe to install unchanged" in normalized
    assert "alternate root" in deployment.lower()
    assert "Crafty token" not in deployment
    assert "Crafty server ID" not in deployment
    assert "provider-specific instance identifiers" in deployment
    assert "tools/quality/" in deployment


def test_operations_and_security_docs_label_reference_values_and_authority() -> None:
    operations = _read("docs/operations-example.md")
    security = _read("docs/security-model.md")
    examples = _read("config/examples/README.md")

    assert "none is a claim about a live host" in operations
    assert "external private deployment overlay" in operations
    assert "cannot select an executable, path, unit" in operations
    assert "not describe a current host" in security
    assert "security and lifecycle invariant ledger" in security
    assert "external private deployment overlay" in examples
    assert "H1/H2/G11" not in examples
