import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOGGER = logging.getLogger()
LOGGER.setLevel(LOG_LEVEL)

TABLE_NAME = os.environ["DDB_TABLE_NAME"]
ACTIVE_REQUESTS_INDEX_NAME = os.environ["ACTIVE_REQUESTS_INDEX_NAME"]
GRACE_HOURS = int(os.getenv("GRACE_HOURS", "8"))
REBOOT_REQUIRED_TAG_KEY = os.environ["REBOOT_REQUIRED_TAG_KEY"]
REBOOT_REQUIRED_TAG_VALUE = os.environ["REBOOT_REQUIRED_TAG_VALUE"]

ec2_client = boto3.client("ec2")
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
deserializer = TypeDeserializer()


def utc_now() -> datetime:
    return datetime.now(UTC)


def isoformat(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    processed = 0
    skipped = 0

    for record in event.get("Records", []):
        if record.get("eventName") not in {"INSERT", "MODIFY"}:
            skipped += 1
            continue

        new_image = record.get("dynamodb", {}).get("NewImage")
        old_image = record.get("dynamodb", {}).get("OldImage")
        if not new_image:
            skipped += 1
            continue

        new_item = deserialize_image(new_image)
        old_item = deserialize_image(old_image) if old_image else {}
        status = new_item.get("status")
        previous_status = old_item.get("status")

        if status not in {"APPROVED", "AUTO_APPROVED"}:
            skipped += 1
            continue

        if previous_status == status:
            skipped += 1
            continue

        if process_request(new_item):
            processed += 1
        else:
            skipped += 1

    return {"processed": processed, "skipped": skipped}


def process_request(item: dict[str, Any]) -> bool:
    latest = table.query(
        KeyConditionExpression=Key("pk").eq(item["pk"]),
        ScanIndexForward=False,
        Limit=1,
    ).get("Items", [])

    if not latest:
        return False

    latest_item = latest[0]
    if latest_item["sk"] != item["sk"]:
        LOGGER.info("ignoring outdated request %s", item["sk"])
        return False

    if latest_item.get("status") not in {"APPROVED", "AUTO_APPROVED"}:
        LOGGER.info("request %s already moved to %s", item["sk"], latest_item.get("status"))
        return False

    instance_id = latest_item.get("instance_id")
    if not instance_id:
        return False

    now = utc_now()
    try:
        ec2_client.create_tags(
            Resources=[instance_id],
            Tags=[{"Key": REBOOT_REQUIRED_TAG_KEY, "Value": REBOOT_REQUIRED_TAG_VALUE}],
        )
    except ClientError as error:
        LOGGER.exception("failed to tag instance %s", instance_id)
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
    return True


def deserialize_image(image: dict[str, Any]) -> dict[str, Any]:
    return {key: deserializer.deserialize(value) for key, value in image.items()}


def build_active_gsi_pk(status: str) -> str:
    return f"ACTIVE#{status}"


def build_active_gsi_sk(*, account_id: str, region: str, instance_id: str, created_at: str) -> str:
    return (
        f"ACCOUNT#{account_id}"
        f"#REGION#{region}"
        f"#INSTANCE#{instance_id}"
        f"#CREATED#{created_at}"
    )
