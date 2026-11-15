"""Notification (WhatsApp) service stack (DEV shape): Lambdas behind notify.sgtvl.rest, no VPC.

Shares the restaurant-mgmt service's Aurora cluster and JWT signing key (SSM parameters published
by that stack). This stack owns:

* the ``sg_notify`` DB-role secret and the WhatsApp **provider secret**
  ``sg/notification-service/whatsapp`` (RETAIN; created with placeholder values + a generated
  ``verify_token`` -- fill ``access_token`` / ``phone_number_id`` / ``app_secret`` by hand),
* the SQS queue ``sg-notify-send`` (fixed name, so producer stacks can build its ARN/URL) and its
  DLQ ``sg-notify-send-dlq`` (after 5 receives); URL/ARN also published to SSM
  ``/sg/notify/queue-url`` and ``/sg/notify/queue-arn``,
* the API Lambda (admin routes + provider webhooks), a one-shot migrate/provision Lambda, the
  SQS-triggered sender (batch 10, partial batch failures, reserved concurrency 5 to respect the
  provider's rate) and the once-a-minute campaign scheduler (EventBridge),
* the custom domain.

Producers (order-service, user-service) are granted ``sqs:SendMessage`` on the queue in THEIR
stacks (ARN ``arn:aws:sqs:<region>:<account>:sg-notify-send``).
"""

from __future__ import annotations

import json
from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_lambda_event_sources as event_sources
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_route53_targets as route53_targets
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_ssm as ssm
from aws_cdk.aws_lambda_python_alpha import BundlingOptions, PythonFunction
from constructs import Construct

# .../notification-service/infra/cdk/stacks/notification_service_stack.py -> parents[3]
SERVICE_DIR = Path(__file__).resolve().parents[3]
DB_NAME = "sg_user"  # shared database

DOMAIN_NAME = "notify.sgtvl.rest"
AUTH_DOMAIN = "auth.sgtvl.rest"  # token issuer (restaurant-mgmt service)
HOSTED_ZONE_ID = "Z082884819C0D5RA4JA10"
HOSTED_ZONE_NAME = "sgtvl.rest"

QUEUE_NAME = "sg-notify-send"
DLQ_NAME = "sg-notify-send-dlq"
WHATSAPP_SECRET_NAME = "sg/notification-service/whatsapp"
SENDER_TIMEOUT = Duration.seconds(30)
SENDER_CONCURRENCY = 5

_BUNDLING_EXCLUDES = [
    ".venv",
    ".git",
    "tests",
    "infra",
    "cdk.out",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    "*.egg-info",
    "docs",
    "openapi",
]


class NotificationServiceStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs: object) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ---- Shared resources imported from the restaurant-mgmt stack (via SSM) ----
        db_host = ssm.StringParameter.value_from_lookup(self, "/sg/shared/db-endpoint")
        admin_secret_arn = ssm.StringParameter.value_from_lookup(
            self, "/sg/shared/admin-secret-arn"
        )
        signing_key_arn = ssm.StringParameter.value_from_lookup(self, "/sg/shared/signing-key-arn")
        signing_key_id = ssm.StringParameter.value_from_lookup(self, "/sg/shared/signing-key-id")

        # ---- Non-superuser notify DB-role credentials (sg_notify) ----
        app_secret = secretsmanager.Secret(
            self,
            "NotifyDbSecret",
            secret_name="sg/notification-service/db-app",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template=json.dumps({"username": "sg_notify"}),
                generate_string_key="password",
                exclude_punctuation=True,
                password_length=32,
            ),
        )

        # ---- WhatsApp provider secret (owned; RETAIN) ----
        # Placeholders only -- never real values in code. Fill access_token / phone_number_id /
        # app_secret in the console (or `aws secretsmanager put-secret-value`) after deploy; the
        # generated verify_token is what you paste into Meta's webhook settings.
        whatsapp_secret = secretsmanager.Secret(
            self,
            "WhatsAppSecret",
            secret_name=WHATSAPP_SECRET_NAME,
            description="WhatsApp provider config for notification-service (JSON)",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template=json.dumps(
                    {
                        "provider": "meta",
                        "access_token": "REPLACE_ME",
                        "phone_number_id": "REPLACE_ME",
                        "app_secret": "REPLACE_ME",
                        "graph_version": "v21.0",
                    }
                ),
                generate_string_key="verify_token",
                exclude_punctuation=True,
                password_length=40,
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ---- Queue + DLQ (fixed names; producers build the ARN deterministically) ----
        dlq = sqs.Queue(
            self,
            "NotifySendDlq",
            queue_name=DLQ_NAME,
            retention_period=Duration.days(14),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
        )
        queue = sqs.Queue(
            self,
            "NotifySendQueue",
            queue_name=QUEUE_NAME,
            # > sender timeout (AWS guidance: 6x the function timeout for SQS event sources)
            visibility_timeout=Duration.seconds(SENDER_TIMEOUT.to_seconds() * 6),
            retention_period=Duration.days(4),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=5, queue=dlq),
        )

        common_lambda = {
            "runtime": _lambda.Runtime.PYTHON_3_13,
            "entry": str(SERVICE_DIR),
            "bundling": BundlingOptions(asset_excludes=_BUNDLING_EXCLUDES),
        }
        common_env = {
            "SG_ENV": "dev",
            "SG_AWS_REGION": self.region,
            # also the "deployed" marker: secret loaders fail closed instead of using dev values
            "SG_KMS_SIGNING_KEY_ID": signing_key_id,
            "SG_JWT_ISSUER": f"https://{AUTH_DOMAIN}",
            "SG_DB_SECRET_ARN": app_secret.secret_arn,
            "SG_DB_HOST": db_host,
            "SG_DB_NAME": DB_NAME,
        }
        # Pass the secret NAME (not a partial ARN): Secrets Manager resolves it to the full ARN,
        # which grant_read's policy matches.
        provider_env = {"SG_NOTIFY_WHATSAPP_SECRET_NAME": WHATSAPP_SECRET_NAME}

        # ---- API Lambda (admin routes + provider webhooks) ----
        fn = PythonFunction(
            self,
            "NotifyFn",
            index="lambda_function.py",
            handler="handler",
            memory_size=512,
            timeout=Duration.seconds(30),
            environment={**common_env, **provider_env},
            **common_lambda,  # type: ignore[arg-type]
        )
        # verify-only (never signs) -- grant on the imported key ARN via an inline policy
        fn.add_to_role_policy(
            iam.PolicyStatement(actions=["kms:GetPublicKey"], resources=[signing_key_arn])
        )
        app_secret.grant_read(fn)
        whatsapp_secret.grant_read(fn)  # webhook verification + STOP confirmation text

        # ---- Sender (SQS -> provider) ----
        sender_fn = PythonFunction(
            self,
            "SenderFn",
            index="sender_function.py",
            handler="handler",
            memory_size=512,
            timeout=SENDER_TIMEOUT,
            reserved_concurrent_executions=SENDER_CONCURRENCY,
            environment={**common_env, **provider_env},
            **common_lambda,  # type: ignore[arg-type]
        )
        app_secret.grant_read(sender_fn)
        whatsapp_secret.grant_read(sender_fn)
        sender_fn.add_event_source(
            event_sources.SqsEventSource(
                queue,
                batch_size=10,
                max_batching_window=Duration.seconds(1),
                report_batch_item_failures=True,
                max_concurrency=SENDER_CONCURRENCY,
            )
        )

        # ---- Scheduler (EventBridge, every minute): due campaigns -> queue ----
        scheduler_fn = PythonFunction(
            self,
            "SchedulerFn",
            index="scheduler_function.py",
            handler="handler",
            memory_size=512,
            timeout=Duration.minutes(5),
            environment={**common_env, "SG_NOTIFY_QUEUE_URL": queue.queue_url},
            **common_lambda,  # type: ignore[arg-type]
        )
        app_secret.grant_read(scheduler_fn)
        queue.grant_send_messages(scheduler_fn)
        events.Rule(
            self,
            "SchedulerTick",
            schedule=events.Schedule.rate(Duration.minutes(1)),
            targets=[targets.LambdaFunction(scheduler_fn, retry_attempts=0)],
        )

        # ---- One-shot migration / role-provisioning Lambda ----
        # Runs the notify chain as the admin user, then grants sg_notify CRUD on the notify
        # tables and the cross-tenant index tables, SELECT/INSERT on the append-only consent.
        # See handlers/migrate_handler.py.
        migrate_fn = PythonFunction(
            self,
            "MigrateFn",
            index="migrate_function.py",
            handler="handler",
            memory_size=512,
            timeout=Duration.minutes(5),
            environment={
                "SG_AWS_REGION": self.region,
                "SG_ADMIN_SECRET_ARN": admin_secret_arn,
                "SG_APP_SECRET_ARN": app_secret.secret_arn,
                "SG_DB_HOST": db_host,
                "SG_DB_NAME": DB_NAME,
            },
            **common_lambda,  # type: ignore[arg-type]
        )
        migrate_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"], resources=[admin_secret_arn]
            )
        )
        app_secret.grant_read(migrate_fn)

        # ---- Published for producer stacks ----
        ssm.StringParameter(
            self, "QueueUrlParam", parameter_name="/sg/notify/queue-url",
            string_value=queue.queue_url,
        )  # fmt: skip
        ssm.StringParameter(
            self, "QueueArnParam", parameter_name="/sg/notify/queue-arn",
            string_value=queue.queue_arn,
        )  # fmt: skip

        # ---- Custom domain (ACM cert + API Gateway domain + Route53 alias) ----
        zone = route53.HostedZone.from_hosted_zone_attributes(
            self, "Zone", hosted_zone_id=HOSTED_ZONE_ID, zone_name=HOSTED_ZONE_NAME
        )
        certificate = acm.Certificate(
            self,
            "ApiCert",
            domain_name=DOMAIN_NAME,
            validation=acm.CertificateValidation.from_dns(zone),
        )
        domain = apigw.DomainName(
            self, "ApiDomain", domain_name=DOMAIN_NAME, certificate=certificate
        )

        http_api = apigw.HttpApi(
            self,
            "NotifyHttpApi",
            api_name="sg-notification-service",
            default_domain_mapping=apigw.DomainMappingOptions(domain_name=domain),
            cors_preflight=apigw.CorsPreflightOptions(
                allow_origins=["*"],
                allow_methods=[apigw.CorsHttpMethod.ANY],
                allow_headers=["authorization", "content-type"],
                max_age=Duration.hours(1),
            ),
        )
        http_api.add_routes(
            path="/{proxy+}",
            methods=[
                apigw.HttpMethod.GET,
                apigw.HttpMethod.POST,
                apigw.HttpMethod.PUT,
                apigw.HttpMethod.PATCH,
                apigw.HttpMethod.DELETE,
            ],
            integration=integrations.HttpLambdaIntegration("NotifyInt", fn),
        )

        route53.ARecord(
            self,
            "ApiAlias",
            zone=zone,
            record_name="notify",
            target=route53.RecordTarget.from_alias(
                route53_targets.ApiGatewayv2DomainProperties(
                    domain.regional_domain_name, domain.regional_hosted_zone_id
                )
            ),
        )

        CfnOutput(self, "CustomUrl", value=f"https://{DOMAIN_NAME}/")
        CfnOutput(self, "ApiUrl", value=http_api.url or "")
        CfnOutput(self, "WebhookUrl", value=f"https://{DOMAIN_NAME}/webhooks/whatsapp/meta")
        CfnOutput(self, "MigrateFunctionName", value=migrate_fn.function_name)
        CfnOutput(self, "NotifyAppSecretArn", value=app_secret.secret_arn)
        CfnOutput(self, "WhatsAppSecretName", value=WHATSAPP_SECRET_NAME)
        CfnOutput(self, "QueueUrl", value=queue.queue_url)
        CfnOutput(self, "QueueArn", value=queue.queue_arn)
        CfnOutput(self, "DlqUrl", value=dlq.queue_url)
