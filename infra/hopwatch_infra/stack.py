"""The AWS side of Hopwatch: tables, the search queue, workers, schedule, alarms.

What is deliberately absent matters as much as what is here:

* **No VPC.** The Lambdas only talk to public AWS endpoints and Wizz Air. A
  VPC would need a NAT gateway for that, which alone costs more per month
  than the rest of the stack combined.
* **No IAM user or access key.** The booker at home needs credentials, but a
  long-lived key minted by a template ends up in CloudFormation's hands. The
  stack publishes a managed policy; you attach it to a user or role yourself.
* **No browser.** Approvals and bookings stay with the booker at home.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as sources
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from hopwatch.aws.schema import TABLES, TableSpec, table_name

SEARCH_INTERVAL_MIN = 15
WORKER_TIMEOUT_S = 240
# SQS visibility must cover the worker timeout; AWS suggests several times it
# so throttled retries do not double-deliver. It is also how long recovery
# from a worker crash takes, so it is no higher than it needs to be.
VISIBILITY_S = 900
MAX_RECEIVES = 5
# Two concurrent workers saturate the shared Wizz rate limit; more would only
# queue up behind it and bill for the wait.
WORKER_CONCURRENCY = 2

_KEY_TYPES = {"S": dynamodb.AttributeType.STRING, "N": dynamodb.AttributeType.NUMBER}


def _attr(key: tuple[str, str]) -> dynamodb.Attribute:
    return dynamodb.Attribute(name=key[0], type=_KEY_TYPES[key[1]])


class HopwatchStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        lambda_code_dir: str,
        alert_email: str | None = None,
        prefix: str = "hopwatch",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        tables = {name: self._table(prefix, spec) for name, spec in TABLES.items()}

        dlq = sqs.Queue(
            self, "SearchesDlq",
            queue_name=f"{prefix}-searches-dlq",
            retention_period=Duration.days(14),
        )
        queue = sqs.Queue(
            self, "Searches",
            queue_name=f"{prefix}-searches",
            visibility_timeout=Duration.seconds(VISIBILITY_S),
            receive_message_wait_time=Duration.seconds(20),
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=MAX_RECEIVES, queue=dlq),
        )

        environment = {
            "HOPWATCH_BACKEND": "aws",
            "HOPWATCH_TABLE_PREFIX": prefix,
            "HOPWATCH_QUEUE_URL": queue.queue_url,
            "HOPWATCH_DLQ_URL": dlq.queue_url,
            "HOPWATCH_LOG_FORMAT": "json",
        }
        code = lambda_.Code.from_asset(lambda_code_dir)

        worker = self._function(
            "Worker", prefix, code, environment,
            handler="hopwatch.aws.lambdas.worker_handler",
            timeout=Duration.seconds(WORKER_TIMEOUT_S),
            reserved_concurrent_executions=WORKER_CONCURRENCY,
        )
        worker.add_event_source(
            sources.SqsEventSource(queue, batch_size=1, report_batch_item_failures=True)
        )

        planner = self._function(
            "Planner", prefix, code, environment,
            handler="hopwatch.aws.lambdas.planner_handler",
            timeout=Duration.seconds(60),
        )
        events.Rule(
            self, "Schedule",
            schedule=events.Schedule.rate(Duration.minutes(SEARCH_INTERVAL_MIN)),
            targets=[targets.LambdaFunction(planner)],
        )

        for table in tables.values():
            table.grant_read_write_data(worker)
            table.grant_read_write_data(planner)
        queue.grant_send_messages(planner)

        booker = iam.ManagedPolicy(
            self, "BookerPolicy",
            managed_policy_name=f"{prefix}-booker",
            description="What the booker at home needs: state, the queue and its depth.",
        )
        booker.add_statements(
            iam.PolicyStatement(
                actions=[
                    "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
                    "dynamodb:DeleteItem", "dynamodb:Query", "dynamodb:Scan",
                    "dynamodb:ConditionCheckItem",
                ],
                resources=[
                    arn
                    for table in tables.values()
                    for arn in (table.table_arn, f"{table.table_arn}/index/*")
                ],
            ),
            iam.PolicyStatement(
                actions=[
                    "sqs:SendMessage", "sqs:ReceiveMessage", "sqs:DeleteMessage",
                    "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes",
                ],
                resources=[queue.queue_arn, dlq.queue_arn],
            ),
        )

        topic = sns.Topic(self, "Alerts", topic_name=f"{prefix}-alerts")
        if alert_email:
            topic.add_subscription(subscriptions.EmailSubscription(alert_email))
        self._alarms(topic, queue, dlq, worker, planner)

        CfnOutput(self, "QueueUrl", value=queue.queue_url)
        CfnOutput(self, "DlqUrl", value=dlq.queue_url)
        CfnOutput(self, "TablePrefix", value=prefix)
        CfnOutput(self, "BookerPolicyArn", value=booker.managed_policy_arn)
        CfnOutput(self, "AlertTopicArn", value=topic.topic_arn)

    # --- pieces -------------------------------------------------------------

    def _table(self, prefix: str, spec: TableSpec) -> dynamodb.Table:
        table = dynamodb.Table(
            self, f"{spec.name.title().replace('_', '')}Table",
            table_name=table_name(prefix, spec.name),
            partition_key=_attr(spec.pk),
            sort_key=_attr(spec.sk) if spec.sk else None,
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute=spec.ttl,
            # Wants and bookings cannot be rebuilt; never delete them along
            # with the stack.
            removal_policy=RemovalPolicy.RETAIN,
        )
        for gsi in spec.gsis:
            table.add_global_secondary_index(
                index_name=gsi.name,
                partition_key=_attr(gsi.pk),
                sort_key=_attr(gsi.sk),
                projection_type=dynamodb.ProjectionType.ALL,
            )
        return table

    def _function(
        self,
        name: str,
        prefix: str,
        code: lambda_.Code,
        environment: dict[str, str],
        **kwargs,
    ) -> lambda_.Function:
        log_group = logs.LogGroup(
            self, f"{name}Logs",
            log_group_name=f"/aws/lambda/{prefix}-{name.lower()}",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )
        return lambda_.Function(
            self, name,
            function_name=f"{prefix}-{name.lower()}",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            memory_size=256,
            code=code,
            environment=environment,
            log_group=log_group,
            **kwargs,
        )

    def _alarms(
        self,
        topic: sns.Topic,
        queue: sqs.Queue,
        dlq: sqs.Queue,
        worker: lambda_.Function,
        planner: lambda_.Function,
    ) -> None:
        five = Duration.minutes(5)
        sweep = Duration.minutes(SEARCH_INTERVAL_MIN)
        above = cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD
        at_least = cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD
        quiet = cloudwatch.TreatMissingData.NOT_BREACHING
        definitions = [
            (
                "DeadLetters",
                "Search jobs failed five times and were dead-lettered.",
                dlq.metric_approximate_number_of_messages_visible(period=five, statistic="Maximum"),
                0, above, quiet,
            ),
            (
                "QueueStalled",
                "A search job has waited twice the sweep interval. Are workers running?",
                queue.metric_approximate_age_of_oldest_message(period=five, statistic="Maximum"),
                2 * SEARCH_INTERVAL_MIN * 60, above, quiet,
            ),
            (
                "WorkerErrors",
                "Search workers are failing repeatedly.",
                worker.metric_errors(period=sweep, statistic="Sum"),
                3, at_least, quiet,
            ),
            (
                "PlannerErrors",
                "The planner could not queue this slot's sweeps.",
                planner.metric_errors(period=sweep, statistic="Sum"),
                1, at_least, quiet,
            ),
            (
                "BookerSilent",
                "The booker at home has not reported in for 15 minutes; nothing can be booked.",
                cloudwatch.Metric(
                    namespace="Hopwatch",
                    metric_name="BookerHeartbeatAgeSeconds",
                    period=sweep,
                    statistic="Maximum",
                ),
                # No data means the booker never reported at all: the worst
                # case, not a quiet one.
                900, above, cloudwatch.TreatMissingData.BREACHING,
            ),
        ]
        action = cw_actions.SnsAction(topic)
        for name, description, metric, threshold, comparison, missing in definitions:
            alarm = cloudwatch.Alarm(
                self, name,
                alarm_name=f"hopwatch-{name}",
                alarm_description=description,
                metric=metric,
                threshold=threshold,
                comparison_operator=comparison,
                evaluation_periods=1,
                treat_missing_data=missing,
            )
            alarm.add_alarm_action(action)
            alarm.add_ok_action(action)
