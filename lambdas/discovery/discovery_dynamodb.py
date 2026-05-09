import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

LOGGER = logging.getLogger(__name__)

TABLE_NAME = os.environ["DDB_TABLE_NAME"]
ACTIVE_REQUESTS_INDEX_NAME = os.environ["ACTIVE_REQUESTS_INDEX_NAME"]
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "60"))

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)


def isoformat(value: datetime) -> str:
    """Format a datetime in the timestamp shape used across DynamoDB items."""
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ttl_epoch(value: datetime) -> int:
    """Convert a datetime into the TTL epoch used to expire old requests."""
    return int((value + timedelta(days=RETENTION_DAYS)).timestamp())


def summarize_metadata_changes(
    item: dict[str, Any], changes: dict[str, Any]
) -> list[str]:
    """Summarize only the metadata fields that changed during a request refresh."""
    summary: list[str] = []
    tracked_fields = (
        "hostname",
        "owner",
        "environment",
        "patch_install_window",
        "patch_install_window_description",
        "next_install_window_at",
    )
    for field in tracked_fields:
        if field not in changes:
            continue
        previous_value = item.get(field)
        next_value = changes.get(field)
        if previous_value == next_value:
            continue
        summary.append(f"{field}: {previous_value} -> {next_value}")
    return summary


def fetch_active_requests(
    region: str, active_statuses: tuple[str, ...]
) -> dict[str, dict[str, Any]]:
    """Load the newest active request per instance for the lambda's current region."""
    results: dict[str, dict[str, Any]] = {}

    for status in active_statuses:
        next_key = None
        status_count = 0
        skipped_other_regions = 0
        while True:
            request: dict[str, Any] = {
                "IndexName": ACTIVE_REQUESTS_INDEX_NAME,
                "KeyConditionExpression": Key("gsi1pk").eq(build_active_gsi_pk(status)),
            }
            if next_key:
                request["ExclusiveStartKey"] = next_key

            response = table.query(**request)
            for item in response.get("Items", []):
                if item.get("region") != region:
                    skipped_other_regions += 1
                    continue
                instance_id = item.get("instance_id")
                if not instance_id:
                    continue
                existing = results.get(instance_id)
                if not existing or item["sk"] > existing["sk"]:
                    results[instance_id] = item
                status_count += 1

            next_key = response.get("LastEvaluatedKey")
            if not next_key:
                break
        LOGGER.debug(
            "fetched active requests region=%s status=%s items=%s skipped_other_regions=%s",
            region,
            status,
            status_count,
            skipped_other_regions,
        )

    LOGGER.info(
        "fetched active request instances region=%s total=%s", region, len(results)
    )
    return results


def fetch_latest_request(
    account_id: str, region: str, instance_id: str
) -> dict[str, Any] | None:
    """Fetch the newest request row for one instance, regardless of current status."""
    response = table.query(
        KeyConditionExpression=Key("pk").eq(build_pk(account_id, region, instance_id)),
        ScanIndexForward=False,
        Limit=1,
    )
    items = response.get("Items", [])
    LOGGER.debug(
        "fetched latest request instance_id=%s found=%s", instance_id, bool(items)
    )
    return items[0] if items else None


def put_new_request(
    *,
    status: str,
    fields: dict[str, Any],
    now: datetime,
    active_statuses: tuple[str, ...],
) -> None:
    """Insert a brand-new request row into DynamoDB with its initial workflow state."""
    request_id = str(uuid.uuid4())
    timestamp = isoformat(now)
    item = {
        "pk": build_pk(fields["account_id"], fields["region"], fields["instance_id"]),
        "sk": f"INSTALL#{timestamp}#{request_id}",
        "request_id": request_id,
        "status": status,
        "postpone_count": 0,
        "postponed_until": None,
        "approved_by": None,
        "approved_at": None,
        "ready_for_install_at": None,
        "resolution_reason": None,
        "last_error": None,
        "created_at": timestamp,
        "updated_at": timestamp,
        "expires_at": ttl_epoch(now),
        **fields,
    }
    apply_active_index_fields(item, status, active_statuses)
    table.put_item(Item=item)
    LOGGER.info(
        "created request request_id=%s instance_id=%s status=%s",
        request_id,
        fields["instance_id"],
        status,
    )


def update_request(
    item: dict[str, Any], changes: dict[str, Any], active_statuses: tuple[str, ...]
) -> None:
    """Apply a partial update to an existing request and keep the active-request GSI in sync."""
    next_item = {**item, **changes}
    status = next_item["status"]
    apply_active_index_fields(next_item, status, active_statuses)
    metadata_changes = summarize_metadata_changes(item, changes)

    update_parts = []
    remove_parts = []
    expression_names: dict[str, str] = {}
    expression_values: dict[str, Any] = {}

    keys = set(changes.keys()) | {"gsi1pk", "gsi1sk"}
    for index, key in enumerate(sorted(keys)):
        name_key = f"#n{index}"
        expression_names[name_key] = key
        value = next_item.get(key)
        if value is None:
            remove_parts.append(name_key)
            continue
        value_key = f":v{index}"
        expression_values[value_key] = value
        update_parts.append(f"{name_key} = {value_key}")

    expressions = []
    if update_parts:
        expressions.append("SET " + ", ".join(update_parts))
    if remove_parts:
        expressions.append("REMOVE " + ", ".join(remove_parts))

    table.update_item(
        Key={"pk": item["pk"], "sk": item["sk"]},
        UpdateExpression=" ".join(expressions),
        ExpressionAttributeNames=expression_names,
        ExpressionAttributeValues=expression_values if expression_values else None,
    )
    if item.get("status") == next_item.get("status"):
        if metadata_changes:
            LOGGER.info(
                "request %s for instance %s kept status %s; updated metadata: %s",
                item.get("request_id"),
                item.get("instance_id"),
                next_item.get("status"),
                ", ".join(metadata_changes),
            )
        else:
            LOGGER.info(
                "request %s for instance %s kept status %s; no relevant metadata changed",
                item.get("request_id"),
                item.get("instance_id"),
                next_item.get("status"),
            )
        return

    LOGGER.info(
        "request %s for instance %s changed status %s -> %s",
        item.get("request_id"),
        item.get("instance_id"),
        item.get("status"),
        next_item.get("status"),
    )


def apply_active_index_fields(
    item: dict[str, Any], status: str, active_statuses: tuple[str, ...]
) -> None:
    """Populate or clear the active-request GSI fields based on the request status."""
    if status in active_statuses:
        item["gsi1pk"] = build_active_gsi_pk(status)
        item["gsi1sk"] = build_active_gsi_sk(
            account_id=item["account_id"],
            region=item["region"],
            instance_id=item["instance_id"],
            created_at=item["created_at"],
        )
        return

    item["gsi1pk"] = None
    item["gsi1sk"] = None


def build_pk(account_id: str, region: str, instance_id: str) -> str:
    """Build the partition key used to group all request rows for one instance in one region."""
    return f"ACCOUNT#{account_id}#REGION#{region}#INSTANCE#{instance_id}"


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
