import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(LOG_LEVEL)

TABLE_NAME = os.environ["DDB_TABLE_NAME"]
ACTIVE_REQUESTS_INDEX_NAME = os.environ["ACTIVE_REQUESTS_INDEX_NAME"]
GRACE_HOURS = int(os.getenv("GRACE_HOURS", "8"))
REBOOT_REQUIRED_TAG_KEY = os.environ["REBOOT_REQUIRED_TAG_KEY"]
REBOOT_REQUIRED_TAG_VALUE = os.environ["REBOOT_REQUIRED_TAG_VALUE"]
EXECUTABLE_STATUSES = {"APPROVED", "AUTO_APPROVED"}
CURRENT_REGION = os.environ["AWS_REGION"]

ec2_client = boto3.client("ec2")
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
deserializer = TypeDeserializer()


def utc_now() -> datetime:
    """Return the current time in UTC."""
    return datetime.now(UTC)


def isoformat(value: datetime) -> str:
    """Format a datetime in the timestamp shape used across DynamoDB items."""
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


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


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Process DynamoDB Stream records that move requests into an executable status."""
    # Executor reacts only to DynamoDB Stream transitions that mean
    # "this request is now approved to be executed".
    processed = 0
    skipped = 0
    request_id = getattr(context, "aws_request_id", "unknown")
    records = event.get("Records", [])

    LOGGER.info("executor started request_id=%s records=%s", request_id, len(records))

    for index, record in enumerate(records, start=1):
        event_name = record.get("eventName")
        record_id = record.get("eventID", f"record-{index}")
        if event_name not in {"INSERT", "MODIFY"}:
            log_record_action(
                "SKIP_RECORD",
                level=logging.DEBUG,
                reason="UNSUPPORTED_EVENT_NAME",
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
                reason="MISSING_NEW_IMAGE",
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
            extra={"event_name": event_name, "record_id": record_id, "region": item_region},
        )

        if item_region != CURRENT_REGION:
            log_record_action(
                "SKIP_RECORD",
                request_id=item_request_id,
                instance_id=instance_id,
                previous_status=previous_status,
                next_status=status,
                reason="REQUEST_BELONGS_TO_ANOTHER_REGION",
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
                reason="STATUS_NOT_ELIGIBLE_FOR_EXECUTION",
            )
            skipped += 1
            continue

        if previous_status == status:
            # Stream records can be replayed or generated by metadata-only updates.
            # We only act on a real status transition into an executable state.
            log_record_action(
                "SKIP_RECORD",
                request_id=item_request_id,
                instance_id=instance_id,
                previous_status=previous_status,
                next_status=status,
                reason="STATUS_DID_NOT_CHANGE",
            )
            skipped += 1
            continue

        if process_request(new_item):
            processed += 1
        else:
            skipped += 1

    result = {"processed": processed, "skipped": skipped}
    LOGGER.info("executor finished request_id=%s result=%s", request_id, result)
    return result


def process_request(item: dict[str, Any]) -> bool:
    """Validate the latest request, tag the instance, and move the request to TAGGED_FOR_REBOOT."""
    # Before changing EC2 or DynamoDB, re-read the latest row from DynamoDB.
    # This protects the executor from acting on stale stream records.
    request_id = item.get("request_id")
    instance_id = item.get("instance_id")
    requested_status = item.get("status")
    requested_region = item.get("region")
    latest = table.query(
        KeyConditionExpression=Key("pk").eq(item["pk"]),
        ScanIndexForward=False,
        Limit=1,
    ).get("Items", [])

    if not latest:
        log_record_action(
            "SKIP_REQUEST",
            request_id=request_id,
            instance_id=instance_id,
            next_status=requested_status,
            reason="LATEST_REQUEST_NOT_FOUND",
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
            reason="OUTDATED_STREAM_RECORD",
            extra={"latest_sk": latest_item["sk"], "stream_sk": item["sk"]},
        )
        return False

    if latest_region != CURRENT_REGION:
        log_record_action(
            "SKIP_REQUEST",
            request_id=request_id,
            instance_id=instance_id,
            next_status=requested_status,
            reason="LATEST_REQUEST_BELONGS_TO_ANOTHER_REGION",
            extra={"request_region": latest_region, "lambda_region": CURRENT_REGION},
        )
        return False

    if latest_item.get("status") not in EXECUTABLE_STATUSES:
        log_record_action(
            "SKIP_REQUEST",
            request_id=request_id,
            instance_id=latest_item.get("instance_id"),
            previous_status=requested_status,
            next_status=latest_item.get("status"),
            reason="REQUEST_ALREADY_MOVED",
        )
        return False

    instance_id = latest_item.get("instance_id")
    if not instance_id:
        log_record_action(
            "SKIP_REQUEST",
            request_id=request_id,
            next_status=requested_status,
            reason="MISSING_INSTANCE_ID",
        )
        return False

    now = utc_now()
    try:
        # Tagging is the signal consumed by the reboot automation outside this Lambda.
        ec2_client.create_tags(
            Resources=[instance_id],
            Tags=[{"Key": REBOOT_REQUIRED_TAG_KEY, "Value": REBOOT_REQUIRED_TAG_VALUE}],
        )
        log_record_action(
            "TAG_INSTANCE",
            request_id=request_id,
            instance_id=instance_id,
            previous_status=latest_item.get("status"),
            next_status="TAGGED_FOR_REBOOT",
            reason="REBOOT_REQUIRED_TAG_APPLIED",
        )
    except ClientError as error:
        log_record_action(
            "TAG_INSTANCE_FAILED",
            level=logging.ERROR,
            request_id=request_id,
            instance_id=instance_id,
            previous_status=latest_item.get("status"),
            next_status=requested_status,
            reason="FAILED_TO_APPLY_REBOOT_REQUIRED_TAG",
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

    grace_until = now + timedelta(hours=GRACE_HOURS)
    # Once the instance is tagged, the request moves to TAGGED_FOR_REBOOT and starts its grace window.
    table.update_item(
        Key={"pk": latest_item["pk"], "sk": latest_item["sk"]},
        UpdateExpression=(
            "SET #status = :status, "
            "gsi1pk = :gsi1pk, "
            "gsi1sk = :gsi1sk, "
            "tagged_for_reboot_at = :tagged_for_reboot_at, "
            "grace_until = :grace_until, "
            "updated_at = :updated_at, "
            "last_error = :last_error"
        ),
        ConditionExpression="#status = :approved OR #status = :auto_approved",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":status": "TAGGED_FOR_REBOOT",
            ":gsi1pk": build_active_gsi_pk("TAGGED_FOR_REBOOT"),
            ":gsi1sk": build_active_gsi_sk(
                account_id=latest_item["account_id"],
                region=latest_item["region"],
                instance_id=latest_item["instance_id"],
                created_at=latest_item["created_at"],
            ),
            ":approved": "APPROVED",
            ":auto_approved": "AUTO_APPROVED",
            ":tagged_for_reboot_at": isoformat(now),
            ":grace_until": isoformat(grace_until),
            ":updated_at": isoformat(now),
            ":last_error": None,
        },
    )
    log_record_action(
        "UPDATE_REQUEST",
        request_id=request_id,
        instance_id=instance_id,
        previous_status=latest_item.get("status"),
        next_status="TAGGED_FOR_REBOOT",
        reason="EXECUTION_ACCEPTED_AND_GRACE_PERIOD_STARTED",
        extra={"grace_until": isoformat(grace_until)},
    )
    return True


def deserialize_image(image: dict[str, Any]) -> dict[str, Any]:
    """Convert a DynamoDB Stream image into a regular Python dictionary."""
    return {key: deserializer.deserialize(value) for key, value in image.items()}


def build_active_gsi_pk(status: str) -> str:
    """Build the GSI partition key used to query active requests by status."""
    return f"ACTIVE#{status}"


def build_active_gsi_sk(*, account_id: str, region: str, instance_id: str, created_at: str) -> str:
    """Build the GSI sort key that keeps active requests grouped by account, region, and instance."""
    return (
        f"ACCOUNT#{account_id}"
        f"#REGION#{region}"
        f"#INSTANCE#{instance_id}"
        f"#CREATED#{created_at}"
    )
