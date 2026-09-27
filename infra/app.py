"""CDK app: ``cd infra && npx aws-cdk@2 synth`` (or ``deploy``, when you mean it)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import aws_cdk as cdk

from hopwatch_infra.oidc import GithubOidcStack
from hopwatch_infra.stack import HopwatchStack

ROOT = Path(__file__).resolve().parent.parent
LAMBDA_BUNDLE = ROOT / "build" / "lambda"

if not (LAMBDA_BUNDLE / "hopwatch").is_dir():
    sys.exit(f"No Lambda bundle at {LAMBDA_BUNDLE}. Run scripts/build_lambda.sh first.")

app = cdk.App()
env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=app.node.try_get_context("region") or "eu-central-1",
)
HopwatchStack(
    app, "HopwatchStack",
    lambda_code_dir=str(LAMBDA_BUNDLE),
    alert_email=app.node.try_get_context("alert_email"),
    env=env,
)
GithubOidcStack(app, "HopwatchGithubOidc", env=env)
app.synth()
