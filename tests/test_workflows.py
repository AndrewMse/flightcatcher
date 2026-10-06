"""CI publishes what the home deployment actually pulls."""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def test_image_is_built_for_the_pi_as_well() -> None:
    """The README targets a Raspberry Pi; an amd64-only image will not start there."""
    ci = yaml.safe_load((WORKFLOWS / "ci.yml").read_text())
    build = next(
        step for step in ci["jobs"]["image"]["steps"]
        if str(step.get("uses", "")).startswith("docker/build-push-action")
    )
    platforms = {p.strip() for p in build["with"]["platforms"].split(",")}
    assert {"linux/amd64", "linux/arm64"} <= platforms
    assert any(
        str(step.get("uses", "")).startswith("docker/setup-qemu-action")
        for step in ci["jobs"]["image"]["steps"]
    )


def test_deploy_is_skipped_until_opted_in() -> None:
    deploy = yaml.safe_load((WORKFLOWS / "deploy-aws.yml").read_text())
    assert "vars.AWS_DEPLOY_ROLE_ARN != ''" in deploy["jobs"]["deploy"]["if"]
