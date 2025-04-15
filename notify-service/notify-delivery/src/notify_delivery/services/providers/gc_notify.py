# Copyright © 2022 Province of British Columbia
#
# Licensed under the Apache License, Version 2.0 (the 'License');
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an 'AS IS' BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""This provides send email through GC Notify Service."""

import base64

from flask import current_app
from notifications_python_client import NotificationsAPIClient
from notifications_python_client.errors import HTTPError
from notify_api.models import (
    Notification,
    NotificationSendResponse,
    NotificationSendResponses,
)
from structured_logging import StructuredLogging

logger = StructuredLogging.get_logger()


class GCNotify:
    """Send notification via GC Notify service."""

    def __init__(self, notification: Notification):
        """Construct object."""
        self.api_key = current_app.config.get("GC_NOTIFY_API_KEY")
        self.gc_notify_url = current_app.config.get("GC_NOTIFY_API_URL")
        self.gc_notify_template_id = current_app.config.get("GC_NOTIFY_TEMPLATE_ID")
        self.gc_notify_email_reply_to_id = current_app.config.get(
            "GC_NOTIFY_EMAIL_REPLY_TO_ID"
        )
        self.notification = notification

    def send(self) -> NotificationSendResponses:
        """Send email through GC Notify."""
        client = NotificationsAPIClient(
            api_key=self.api_key, base_url=self.gc_notify_url
        )

        deployment_env = current_app.config.get("DEPLOYMENT_ENV", "production").lower()
        content = self.notification.content[0]
        subject = content.subject

        if deployment_env != "production":
            subject += f" - from {deployment_env.upper()} environment"

        email_content = {
            "email_subject": subject,
            "email_body": content.body,
        }

        # Collect attachments if they exist
        if content.attachments:
            email_content.update(
                {
                    f"attachment{idx}": {
                        "file": base64.b64encode(attachment.file_bytes).decode(),
                        "filename": attachment.file_name,
                        "sending_method": "attach",
                    }
                    for idx, attachment in enumerate(
                        content.attachments
                    )
                }
            )

        # Send one email at a time and collect responses
        response_list = []
        for recipient in self.notification.recipients.split(","):
            try:
                response = client.send_email_notification(
                    email_address=recipient,
                    template_id=self.gc_notify_template_id,
                    personalisation=email_content,
                    email_reply_to_id=self.gc_notify_email_reply_to_id,
                )
                response_list.append(
                    NotificationSendResponse(
                        response_id=response["id"], recipient=recipient
                    )
                )
            except (HTTPError, Exception) as e:
                logger.error(f"Error sending email to {recipient}: {e}")

        return NotificationSendResponses(recipients=response_list)
