import json
import logging
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import boto3
from botocore.exceptions import BotoCoreError, ClientError

LOGGER = logging.getLogger(__name__)

TEAMS_WEBHOOK_SECRET_ARN = os.getenv("TEAMS_WEBHOOK_SECRET_ARN")
MESSAGE_CARD_THEME_COLOR = "0078D4"
MESSAGE_CARD_CONTEXT = "https://schema.org/extensions"
MESSAGE_CARD_TYPE = "MessageCard"
HTTP_TIMEOUT_SECONDS = 10

secretsmanager_client = boto3.client("secretsmanager")

_webhook_cache: str | None = None
_webhook_loaded = False


class TeamsNotificationError(Exception):
    """Represent one operational failure while preparing or sending a Teams notification."""

    def __init__(self, reason: str, detail: str | None = None):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


def send_install_request_notification(request_item: dict[str, Any]) -> None:
    """Send a single Teams notification for one newly created install request."""
    webhook_url = load_teams_webhook_url()
    payload = build_message_card(request_item)
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            status_code = getattr(response, "status", None) or response.getcode()
            if status_code >= 400:
                raise TeamsNotificationError(
                    "TEAMS_NOTIFICATION_FAILED",
                    f"teams webhook returned status_code={status_code}",
                )
    except HTTPError as error:
        raise TeamsNotificationError(
            "TEAMS_NOTIFICATION_FAILED",
            f"teams webhook http error status_code={error.code}",
        ) from error
    except URLError as error:
        raise TeamsNotificationError(
            "TEAMS_NOTIFICATION_FAILED",
            f"teams webhook network error={error.reason}",
        ) from error


def load_teams_webhook_url() -> str:
    """Load the Teams webhook URL from Secrets Manager and cache it for container reuse."""
    global _webhook_cache, _webhook_loaded

    if _webhook_loaded:
        if not _webhook_cache:
            raise TeamsNotificationError(
                "TEAMS_WEBHOOK_SECRET_EMPTY",
                "teams webhook secret resolved to an empty value",
            )
        return _webhook_cache

    if not TEAMS_WEBHOOK_SECRET_ARN:
        raise TeamsNotificationError(
            "TEAMS_WEBHOOK_SECRET_MISSING",
            "TEAMS_WEBHOOK_SECRET_ARN is not configured",
        )

    try:
        response = secretsmanager_client.get_secret_value(
            SecretId=TEAMS_WEBHOOK_SECRET_ARN
        )
    except (ClientError, BotoCoreError) as error:
        raise TeamsNotificationError(
            "TEAMS_WEBHOOK_SECRET_MISSING",
            f"failed to read Teams webhook secret: {error}",
        ) from error

    secret_string = response.get("SecretString", "")
    webhook_url = extract_webhook_url(secret_string)
    _webhook_cache = webhook_url
    _webhook_loaded = True

    if not webhook_url:
        raise TeamsNotificationError(
            "TEAMS_WEBHOOK_SECRET_EMPTY",
            "teams webhook secret resolved to an empty value",
        )

    return webhook_url


def extract_webhook_url(secret_string: str) -> str | None:
    """Resolve a webhook URL from a plain-text secret or a small JSON secret payload."""
    if not secret_string:
        return None

    normalized = secret_string.strip()
    if not normalized:
        return None

    if normalized.startswith("{"):
        try:
            payload = json.loads(normalized)
        except json.JSONDecodeError:
            LOGGER.warning("teams webhook secret looks like json but could not be parsed")
            return normalized

        for key in ("webhook_url", "webhookUrl", "url"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    return normalized


def build_message_card(request_item: dict[str, Any]) -> dict[str, Any]:
    """Build a Teams MessageCard payload with the request details needed by operations."""
    instance_id = request_item.get("instance_id", "unknown")
    hostname = request_item.get("hostname") or instance_id
    status = request_item.get("status", "UNKNOWN")
    account_id = request_item.get("account_id", "unknown")
    region = request_item.get("region", "unknown")

    facts = [
        build_fact("request_id", request_item.get("request_id")),
        build_fact("status", status),
        build_fact("account_id", account_id),
        build_fact("region", region),
        build_fact("instance_id", instance_id),
        build_fact("hostname", hostname),
        build_fact("environment", request_item.get("environment")),
        build_fact("patch_severity", request_item.get("patch_severity")),
        build_fact(
            "critical_missing_count", request_item.get("critical_missing_count")
        ),
        build_fact(
            "security_missing_count", request_item.get("security_missing_count")
        ),
        build_fact("other_missing_count", request_item.get("other_missing_count")),
        build_fact("patch_install_window", request_item.get("patch_install_window")),
        build_fact(
            "patch_install_window_description",
            request_item.get("patch_install_window_description"),
        ),
        build_fact("next_install_window_at", request_item.get("next_install_window_at")),
        build_fact("created_at", request_item.get("created_at")),
    ]

    return {
        "@context": MESSAGE_CARD_CONTEXT,
        "@type": MESSAGE_CARD_TYPE,
        "themeColor": MESSAGE_CARD_THEME_COLOR,
        "summary": f"Pending patch updates for {hostname}",
        "title": f"Pending patch updates for {hostname}",
        "text": (
            "A non-compliant instance requires patch installation approval or manual review."
        ),
        "sections": [
            {
                "activityTitle": f"Instance {instance_id} has updates pending",
                "facts": facts,
                "markdown": True,
            }
        ],
    }


def build_fact(name: str, value: Any) -> dict[str, str]:
    """Build one MessageCard fact while normalizing empty values."""
    if value is None or value == "":
        normalized = "-"
    else:
        normalized = str(value)
    return {"name": name, "value": normalized}
