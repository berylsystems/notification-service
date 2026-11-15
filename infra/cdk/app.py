#!/usr/bin/env python3
"""CDK app for the SG notification (WhatsApp) service."""

from __future__ import annotations

import aws_cdk as cdk
from stacks.notification_service_stack import NotificationServiceStack

app = cdk.App()

NotificationServiceStack(
    app,
    "SgNotificationServiceDev",
    env=cdk.Environment(account="659551386596", region="ap-south-1"),  # Mumbai
)

app.synth()
