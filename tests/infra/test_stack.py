"""The CDK stack, asserted on its synthesized CloudFormation. Nothing is deployed."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

cdk = pytest.importorskip("aws_cdk")
from aws_cdk.assertions import Match, Template  # noqa: E402

from hopwatch.aws.schema import TABLES  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "infra"))
from hopwatch_infra.oidc import GithubOidcStack  # noqa: E402
from hopwatch_infra.stack import HopwatchStack  # noqa: E402


@pytest.fixture(scope="module")
def code_dir(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("lambda")
    (path / "placeholder.py").write_text("")
    return str(path)


def synth(code_dir: str, **kwargs) -> Template:
    app = cdk.App()
    stack = HopwatchStack(
        app, "HopwatchStack", lambda_code_dir=code_dir,
        env=cdk.Environment(account="123456789012", region="eu-central-1"), **kwargs,
    )
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def template(code_dir) -> Template:
    return synth(code_dir)


def test_queue_has_dlq_redrive_after_5_receives(template) -> None:
    template.has_resource_properties("AWS::SQS::Queue", {
        "QueueName": "hopwatch-searches",
        "VisibilityTimeout": 900,
        "ReceiveMessageWaitTimeSeconds": 20,
        "RedrivePolicy": Match.object_like({"maxReceiveCount": 5}),
    })
    template.has_resource_properties("AWS::SQS::Queue", {
        "QueueName": "hopwatch-searches-dlq",
        "MessageRetentionPeriod": 14 * 24 * 3600,
    })


def test_worker_lambda_shape(template) -> None:
    template.has_resource_properties("AWS::Lambda::Function", {
        "Handler": "hopwatch.aws.lambdas.worker_handler",
        "Runtime": "python3.12",
        "Architectures": ["arm64"],
        "MemorySize": 256,
        "Timeout": 240,
        "ReservedConcurrentExecutions": 2,
        "Environment": {"Variables": Match.object_like({
            "HOPWATCH_BACKEND": "aws",
            "HOPWATCH_TABLE_PREFIX": "hopwatch",
            "HOPWATCH_LOG_FORMAT": "json",
        })},
    })
    template.has_resource_properties("AWS::Lambda::EventSourceMapping", {
        "BatchSize": 1,
        "FunctionResponseTypes": ["ReportBatchItemFailures"],
    })


def test_planner_runs_every_15_minutes(template) -> None:
    template.has_resource_properties("AWS::Lambda::Function", {
        "Handler": "hopwatch.aws.lambdas.planner_handler",
        "Architectures": ["arm64"],
    })
    template.has_resource_properties("AWS::Events::Rule", {
        "ScheduleExpression": "rate(15 minutes)",
    })


def test_five_alarms_all_notify_sns(template) -> None:
    template.resource_count_is("AWS::CloudWatch::Alarm", 5)
    alarms = template.find_resources("AWS::CloudWatch::Alarm")
    for alarm in alarms.values():
        assert alarm["Properties"]["AlarmActions"], alarm
    heartbeat = [
        a["Properties"] for a in alarms.values()
        if a["Properties"].get("MetricName") == "BookerHeartbeatAgeSeconds"
    ]
    assert heartbeat and heartbeat[0]["TreatMissingData"] == "breaching"
    assert heartbeat[0]["Threshold"] == 900


def test_log_retention_14_days(template) -> None:
    groups = template.find_resources("AWS::Logs::LogGroup")
    assert len(groups) == 2
    assert all(g["Properties"]["RetentionInDays"] == 14 for g in groups.values())


def test_tables_on_demand_with_ttl(template) -> None:
    tables = template.find_resources("AWS::DynamoDB::Table")
    by_name = {t["Properties"]["TableName"]: t["Properties"] for t in tables.values()}
    assert set(by_name) == {f"hopwatch-{name}" for name in TABLES}
    for spec in TABLES.values():
        props = by_name[f"hopwatch-{spec.name}"]
        assert props["BillingMode"] == "PAY_PER_REQUEST"
        if spec.ttl:
            assert props["TimeToLiveSpecification"] == {"AttributeName": spec.ttl, "Enabled": True}
        gsis = {g["IndexName"] for g in props.get("GlobalSecondaryIndexes", [])}
        assert gsis == {g.name for g in spec.gsis}


def test_no_vpc_or_nat_gateway(template) -> None:
    """A NAT gateway alone would cost more per month than everything else here."""
    template.resource_count_is("AWS::EC2::VPC", 0)
    template.resource_count_is("AWS::EC2::NatGateway", 0)


def test_alert_email_is_optional(code_dir, template) -> None:
    template.resource_count_is("AWS::SNS::Subscription", 0)
    with_email = synth(code_dir, alert_email="me@example.com")
    with_email.has_resource_properties("AWS::SNS::Subscription", {
        "Protocol": "email", "Endpoint": "me@example.com",
    })


def test_booker_gets_a_policy_not_a_user(template) -> None:
    template.resource_count_is("AWS::IAM::User", 0)
    template.resource_count_is("AWS::IAM::AccessKey", 0)
    template.has_resource_properties("AWS::IAM::ManagedPolicy", {
        "ManagedPolicyName": "hopwatch-booker",
    })


def test_oidc_role_is_scoped_to_main() -> None:
    app = cdk.App()
    stack = GithubOidcStack(
        app, "Oidc", env=cdk.Environment(account="123456789012", region="eu-central-1")
    )
    template = Template.from_stack(stack)
    template.resource_count_is("AWS::IAM::OIDCProvider", 1)
    roles = template.find_resources(
        "AWS::IAM::Role", {"Properties": {"RoleName": "hopwatch-github-deploy"}}
    )
    [role] = roles.values()
    condition = role["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]["Condition"]
    assert condition["StringEquals"]["token.actions.githubusercontent.com:sub"] == (
        "repo:AndrewMse/hopwatch:ref:refs/heads/main"
    )
    assert condition["StringEquals"]["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"
