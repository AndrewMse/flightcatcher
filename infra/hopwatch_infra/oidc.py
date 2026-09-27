"""Lets GitHub Actions deploy without any stored AWS keys.

GitHub signs a short-lived OIDC token for each workflow run. AWS trusts it
only for this repository's main branch, and the role it grants can do nothing
but hand over to the CDK bootstrap roles. Deploy this stack once, by hand.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_iam as iam
from constructs import Construct

GITHUB_ISSUER = "token.actions.githubusercontent.com"


class GithubOidcStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        repo: str = "AndrewMse/hopwatch",
        branch: str = "main",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # The native CloudFormation resource, not the custom-resource one: no
        # helper Lambda, and AWS validates GitHub's certificate itself.
        provider = iam.OidcProviderNative(
            self, "GitHub",
            url=f"https://{GITHUB_ISSUER}",
            client_ids=["sts.amazonaws.com"],
        )
        role = iam.Role(
            self, "DeployRole",
            role_name="hopwatch-github-deploy",
            max_session_duration=Duration.hours(1),
            assumed_by=iam.WebIdentityPrincipal(
                provider.oidc_provider_arn,
                conditions={
                    "StringEquals": {
                        f"{GITHUB_ISSUER}:aud": "sts.amazonaws.com",
                        f"{GITHUB_ISSUER}:sub": f"repo:{repo}:ref:refs/heads/{branch}",
                    }
                },
            ),
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["sts:AssumeRole"],
                resources=[f"arn:aws:iam::{self.account}:role/cdk-*"],
            )
        )
        CfnOutput(self, "DeployRoleArn", value=role.role_arn)
