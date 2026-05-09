import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

from executor_constants import (
    EXECUTABLE_STATUSES,
    READY_STATUS_BY_EXECUTABLE,
    REASON_FAILED_TO_APPLY_PATCH_INSTALL_APPROVAL_TAG,
    REASON_FAILED_TO_MARK_REQUEST_READY_FOR_INSTALL,
    REASON_FAILED_TO_QUERY_LATEST_REQUEST,
    REASON_LATEST_REQUEST_BELONGS_TO_ANOTHER_REGION,
    REASON_LATEST_REQUEST_NOT_FOUND,
    REASON_MISSING_INSTANCE_ID,
    REASON_MISSING_NEW_IMAGE,
    REASON_OUTDATED_STREAM_RECORD,
    REASON_PATCH_INSTALL_APPROVAL_TAG_APPLIED,
    REASON_REQUEST_ALREADY_MOVED,
    REASON_REQUEST_BELONGS_TO_ANOTHER_REGION,
    REASON_REQUEST_READY_FOR_INSTALL,
    REASON_STATUS_DID_NOT_CHANGE,
    REASON_STATUS_NOT_ELIGIBLE_FOR_EXECUTION,
    REASON_UNHANDLED_EXCEPTION,
    REASON_UNSUPPORTED_EVENT_NAME,
    STATUS_APPROVED,
    STATUS_AUTO_APPROVED,
)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(LOG_LEVEL)

TABLE_NAME = os.environ["DDB_TABLE_NAME"]
PATCH_INSTALL_APPROVED_TAG_KEY = os.environ["PATCH_INSTALL_APPROVED_TAG_KEY"]
PATCH_INSTALL_APPROVED_TAG_VALUE = os.environ["PATCH_INSTALL_APPROVED_TAG_VALUE"]
INSTALL_GRACE_HOURS = float(os.getenv("INSTALL_GRACE_HOURS", "8"))
CURRENT_REGION = os.environ["AWS_REGION"]

ec2_client = boto3.client("ec2")
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
deserializer = TypeDeserializer()


###########################################
# Shared Helpers
###########################################


def utc_now() -> datetime:
    """Return the current time in UTC."""
    return datetime.now(UTC)


def isoformat(value: datetime) -> str:
    """Format a datetime in the timestamp shape used across DynamoDB items."""
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse an ISO timestamp into a timezone-aware datetime when available."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def compute_install_grace_until(
    expected_install_window_at: str | None, approved_for_install_at: datetime
) -> str:
    """Compute the grace deadline using the expected window when available, otherwise approval time."""
    expected_at = parse_timestamp(expected_install_window_at) or approved_for_install_at
    if expected_at.tzinfo is None:
        expected_at = expected_at.replace(tzinfo=UTC)
    return isoformat(expected_at + timedelta(hours=INSTALL_GRACE_HOURS))


def log_record_action(
    action: str,
    *,
    level: int = logging.INFO,
    request_id: str | None = None,
    instance_id: str | None = None,
    previous_status: str | None = None,
    next_status: str | None = None,
    reason: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Write a structured log line that explains what the executor did with one stream record."""
    details: list[str] = [f"action={action}"]
    if request_id:
        details.append(f"request_id={request_id}")
    if instance_id:
        details.append(f"instance_id={instance_id}")
    if previous_status:
        details.append(f"previous_status={previous_status}")
    if next_status:
        details.append(f"next_status={next_status}")
    if reason:
        details.append(f"reason={reason}")
    if extra:
        for key in sorted(extra):
            details.append(f"{key}={extra[key]}")
    LOGGER.log(level, " ".join(details))


###########################################
# Lambda Handler
###########################################


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Process DynamoDB Stream records that move requests into an executable status."""
    processed = 0
    skipped = 0
    request_id = getattr(context, "aws_request_id", "unknown")
    records = event.get("Records", [])

    LOGGER.info("executor started request_id=%s records=%s", request_id, len(records))

    for index, record in enumerate(records, start=1):
        try:
            event_name = record.get("eventName")
            record_id = record.get("eventID", f"record-{index}")
            if event_name not in {"INSERT", "MODIFY"}:
                log_record_action(
                    "SKIP_RECORD",
                    level=logging.DEBUG,
                    reason=REASON_UNSUPPORTED_EVENT_NAME,
                    extra={"event_name": event_name, "record_id": record_id},
                )
                skipped += 1
                continue

            new_image = record.get("dynamodb", {}).get("NewImage")
            old_image = record.get("dynamodb", {}).get("OldImage")
            if not new_image:
                log_record_action(
                    "SKIP_RECORD",
                    level=logging.WARNING,
                    reason=REASON_MISSING_NEW_IMAGE,
                    extra={"event_name": event_name, "record_id": record_id},
                )
                skipped += 1
                continue

            new_item = deserialize_image(new_image)
            old_item = deserialize_image(old_image) if old_image else {}
            status = new_item.get("status")
            previous_status = old_item.get("status")
            item_request_id = new_item.get("request_id")
            instance_id = new_item.get("instance_id")
            item_region = new_item.get("region")

            log_record_action(
                "RECEIVE_RECORD",
                level=logging.INFO,
                request_id=item_request_id,
                instance_id=instance_id,
                previous_status=previous_status,
                next_status=status,
                extra={
                    "event_name": event_name,
                    "record_id": record_id,
                    "region": item_region,
                },
            )

            if item_region != CURRENT_REGION:
                log_record_action(
                    "SKIP_RECORD",
                    request_id=item_request_id,
                    instance_id=instance_id,
                    previous_status=previous_status,
                    next_status=status,
                    reason=REASON_REQUEST_BELONGS_TO_ANOTHER_REGION,
                    extra={"item_region": item_region, "lambda_region": CURRENT_REGION},
                )
                skipped += 1
                continue

            if status not in EXECUTABLE_STATUSES:
                log_record_action(
                    "SKIP_RECORD",
                    request_id=item_request_id,
                    instance_id=instance_id,
                    previous_status=previous_status,
                    next_status=status,
                    reason=REASON_STATUS_NOT_ELIGIBLE_FOR_EXECUTION,
                )
                skipped += 1
                continue

            if previous_status == status:
                log_record_action(
                    "SKIP_RECORD",
                    request_id=item_request_id,
                    instance_id=instance_id,
                    previous_status=previous_status,
                    next_status=status,
                    reason=REASON_STATUS_DID_NOT_CHANGE,
                )
                skipped += 1
                continue

            if process_request(new_item):
                processed += 1
            else:
                skipped += 1
        except Exception:
            skipped += 1
            LOGGER.exception("failed to process executor record record_index=%s", index)
            log_record_action(
                "RECORD_PROCESSING_FAILED",
                level=logging.ERROR,
                reason=REASON_UNHANDLED_EXCEPTION,
                extra={"record_index": index},
            )

    result = {"processed": processed, "skipped": skipped}
    LOGGER.info("executor finished request_id=%s result=%s", request_id, result)
    return result


###########################################
# Functions of Request Execution
###########################################


def process_request(item: dict[str, Any]) -> bool:
    """Validate the latest request, apply the install-approval tag, and mark it ready for install."""
    request_id = item.get("request_id")
    instance_id = item.get("instance_id")
    requested_status = item.get("status")
    requested_region = item.get("region")
    try:
        latest = table.query(
            KeyConditionExpression=Key("pk").eq(item["pk"]),
            ScanIndexForward=False,
            Limit=1,
        ).get("Items", [])
    except ClientError as error:
        log_record_action(
            "SKIP_REQUEST",
            level=logging.ERROR,
            request_id=request_id,
            instance_id=instance_id,
            next_status=requested_status,
            reason=REASON_FAILED_TO_QUERY_LATEST_REQUEST,
            extra={"error": str(error)},
        )
        return False

    if not latest:
        log_record_action(
            "SKIP_REQUEST",
            request_id=request_id,
            instance_id=instance_id,
            next_status=requested_status,
            reason=REASON_LATEST_REQUEST_NOT_FOUND,
        )
        return False

    latest_item = latest[0]
    latest_region = latest_item.get("region")
    if latest_item["sk"] != item["sk"]:
        log_record_action(
            "SKIP_REQUEST",
            request_id=request_id,
            instance_id=instance_id,
            next_status=requested_status,
            reason=REASON_OUTDATED_STREAM_RECORD,
            extra={"latest_sk": latest_item["sk"], "stream_sk": item["sk"]},
        )
        return False

    if latest_region != CURRENT_REGION:
        log_record_action(
            "SKIP_REQUEST",
            request_id=request_id,
            instance_id=instance_id,
            next_status=requested_status,
            reason=REASON_LATEST_REQUEST_BELONGS_TO_ANOTHER_REGION,
            extra={"request_region": latest_region, "lambda_region": CURRENT_REGION},
        )
        return False

    latest_status = latest_item.get("status")
    if latest_status not in EXECUTABLE_STATUSES:
        log_record_action(
            "SKIP_REQUEST",
            request_id=request_id,
            instance_id=latest_item.get("instance_id"),
            previous_status=requested_status,
            next_status=latest_status,
            reason=REASON_REQUEST_ALREADY_MOVED,
        )
        return False

    instance_id = latest_item.get("instance_id")
    if not instance_id:
        log_record_action(
            "SKIP_REQUEST",
            request_id=request_id,
            next_status=requested_status,
            reason=REASON_MISSING_INSTANCE_ID,
        )
        return False

    next_status = READY_STATUS_BY_EXECUTABLE[latest_status]
    now = utc_now()
    expected_install_window_at = latest_item.get("next_install_window_at")
    approved_for_install_at = isoformat(now)
    install_grace_until = compute_install_grace_until(expected_install_window_at, now)
    try:
        ec2_client.create_tags(
            Resources=[instance_id],
            Tags=[
                {
                    "Key": PATCH_INSTALL_APPROVED_TAG_KEY,
                    "Value": PATCH_INSTALL_APPROVED_TAG_VALUE,
                }
            ],
        )
        log_record_action(
            "TAG_INSTANCE",
            request_id=request_id,
            instance_id=instance_id,
            previous_status=latest_status,
            next_status=next_status,
            reason=REASON_PATCH_INSTALL_APPROVAL_TAG_APPLIED,
            extra={
                "expected_install_window_at": expected_install_window_at,
                "install_grace_until": install_grace_until,
            },
        )
    except ClientError as error:
        log_record_action(
            "TAG_INSTANCE_FAILED",
            level=logging.ERROR,
            request_id=request_id,
            instance_id=instance_id,
            previous_status=latest_status,
            next_status=requested_status,
            reason=REASON_FAILED_TO_APPLY_PATCH_INSTALL_APPROVAL_TAG,
            extra={"error": str(error), "region": requested_region},
        )
        table.update_item(
            Key={"pk": latest_item["pk"], "sk": latest_item["sk"]},
            UpdateExpression="SET last_error = :error, updated_at = :updated_at",
            ExpressionAttributeValues={
                ":error": str(error),
                ":updated_at": isoformat(now),
            },
        )
        return False

    try:
        table.update_item(
            Key={"pk": latest_item["pk"], "sk": latest_item["sk"]},
            UpdateExpression=(
                "SET #status = :status, "
                "gsi1pk = :gsi1pk, "
                "gsi1sk = :gsi1sk, "
                "approved_for_install_at = :approved_for_install_at, "
                "expected_install_window_at = :expected_install_window_at, "
                "install_grace_until = :install_grace_until, "
                "ready_for_install_at = :ready_for_install_at, "
                "updated_at = :updated_at, "
                "last_error = :last_error"
            ),
            ConditionExpression="#status = :approved OR #status = :auto_approved",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":status": next_status,
                ":gsi1pk": build_active_gsi_pk(next_status),
                ":gsi1sk": build_active_gsi_sk(
                    account_id=latest_item["account_id"],
                    region=latest_item["region"],
                    instance_id=latest_item["instance_id"],
                    created_at=latest_item["created_at"],
                ),
                ":approved": STATUS_APPROVED,
                ":auto_approved": STATUS_AUTO_APPROVED,
                ":approved_for_install_at": approved_for_install_at,
                ":expected_install_window_at": expected_install_window_at,
                ":install_grace_until": install_grace_until,
                ":ready_for_install_at": isoformat(now),
                ":updated_at": isoformat(now),
                ":last_error": None,
            },
        )
    except ClientError as error:
        log_record_action(
            "UPDATE_REQUEST_FAILED",
            level=logging.ERROR,
            request_id=request_id,
            instance_id=instance_id,
            previous_status=latest_status,
            next_status=next_status,
            reason=REASON_FAILED_TO_MARK_REQUEST_READY_FOR_INSTALL,
            extra={"error": str(error)},
        )
        return False
    log_record_action(
        "UPDATE_REQUEST",
        request_id=request_id,
        instance_id=instance_id,
        previous_status=latest_status,
        next_status=next_status,
        reason=REASON_REQUEST_READY_FOR_INSTALL,
        extra={
            "expected_install_window_at": expected_install_window_at,
            "install_grace_until": install_grace_until,
        },
    )
    return True


###########################################
# Functions of DynamoDB Stream and GSI
###########################################


def deserialize_image(image: dict[str, Any]) -> dict[str, Any]:
    """Convert a DynamoDB Stream image into a regular Python dictionary."""
    return {key: deserializer.deserialize(value) for key, value in image.items()}


def build_active_gsi_pk(status: str) -> str:
    """Build the GSI partition key used to query active requests by status."""
    return f"ACTIVE#{status}"


def build_active_gsi_sk(
    *, account_id: str, region: str, instance_id: str, created_at: str
) -> str:
    """Build the GSI sort key that keeps active requests grouped by account, region, and instance."""
    return (
        f"ACCOUNT#{account_id}"
        f"#REGION#{region}"
        f"#INSTANCE#{instance_id}"
        f"#CREATED#{created_at}"
    )
