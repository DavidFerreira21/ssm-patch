import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from itertools import islice
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOGGER = logging.getLogger()
LOGGER.setLevel(LOG_LEVEL)

TABLE_NAME = os.environ["DDB_TABLE_NAME"]
ACTIVE_REQUESTS_INDEX_NAME = os.environ["ACTIVE_REQUESTS_INDEX_NAME"]
PATCH_MANAGEMENT_TAG_KEY = os.environ["PATCH_MANAGEMENT_TAG_KEY"]
PATCH_MANAGEMENT_TAG_VALUE = os.environ["PATCH_MANAGEMENT_TAG_VALUE"]
PATCH_REBOOT_WINDOW_TAG_KEY = os.environ["PATCH_REBOOT_WINDOW_TAG_KEY"]
REBOOT_REQUIRED_TAG_KEY = os.environ["REBOOT_REQUIRED_TAG_KEY"]
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "60"))
POSTPONE_DAYS = int(os.getenv("POSTPONE_DAYS", "6"))
MAX_POSTPONES = int(os.getenv("MAX_POSTPONES", "1"))

ACTIVE_STATUSES = (
    "PENDING_APPROVAL",
    "POSTPONED",
    "APPROVED",
    "AUTO_APPROVED",
    "TAGGED_FOR_REBOOT",
)

FINAL_STATUSES = (
    "RESOLVED",
    "FAILED_REBOOT",
    "MANUAL",
    "CANCELLED",
    "INSTANCE_NOT_FOUND",
    "FAILED_CONFIGURATION",
    "CANCELLED_OUT_OF_SCOPE",
)

ssm_client = boto3.client("ssm")
ec2_client = boto3.client("ec2")
sts_client = boto3.client("sts")
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)


def utc_now() -> datetime:
    return datetime.now(UTC)


def isoformat(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ttl_epoch(value: datetime) -> int:
    return int((value + timedelta(days=RETENTION_DAYS)).timestamp())


def chunks(items: list[str], size: int):
    iterator = iter(items)
    while True:
        batch = list(islice(iterator, size))
        if not batch:
            break
        yield batch


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    account_id = sts_client.get_caller_identity()["Account"]
    region = os.environ["AWS_REGION"]
    now = utc_now()

    discovered_instance_ids = fetch_non_compliant_instance_ids()
    active_requests = fetch_active_requests()
    tracked_instance_ids = sorted(discovered_instance_ids | set(active_requests.keys()))

    patch_states = fetch_patch_states(tracked_instance_ids)
    instance_details = fetch_instance_details(tracked_instance_ids)

    processed = 0
    for instance_id in tracked_instance_ids:
        process_instance(
            account_id=account_id,
            region=region,
            instance_id=instance_id,
            now=now,
            patch_state=patch_states.get(instance_id),
            instance_data=instance_details.get(instance_id),
            active_request=active_requests.get(instance_id),
        )
        processed += 1

    return {
        "processed_instances": processed,
        "non_compliant_instances": len(discovered_instance_ids),
        "tracked_active_requests": len(active_requests),
    }


def fetch_non_compliant_instance_ids() -> set[str]:
    instance_ids: set[str] = set()
    next_token = None

    while True:
        request: dict[str, Any] = {
            "Filters": [
                {"Key": "ComplianceType", "Values": ["Patch"], "Type": "EQUAL"},
                {"Key": "Status", "Values": ["NON_COMPLIANT"], "Type": "EQUAL"},
            ]
        }
        if next_token:
            request["NextToken"] = next_token

        response = ssm_client.list_compliance_items(**request)
        for item in response.get("ComplianceItems", []):
            resource_id = item.get("ResourceId")
            if resource_id:
                instance_ids.add(resource_id)

        next_token = response.get("NextToken")
        if not next_token:
            return instance_ids


def fetch_active_requests() -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}

    for status in ACTIVE_STATUSES:
        next_key = None
        while True:
            request: dict[str, Any] = {
                "IndexName": ACTIVE_REQUESTS_INDEX_NAME,
                "KeyConditionExpression": Key("gsi1pk").eq(build_active_gsi_pk(status)),
            }
            if next_key:
                request["ExclusiveStartKey"] = next_key

            response = table.query(**request)
            for item in response.get("Items", []):
                instance_id = item.get("instance_id")
                if not instance_id:
                    continue
                existing = results.get(instance_id)
                if not existing or item["sk"] > existing["sk"]:
                    results[instance_id] = item

            next_key = response.get("LastEvaluatedKey")
            if not next_key:
                break

    return results


def fetch_patch_states(instance_ids: list[str]) -> dict[str, dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    for batch in chunks(instance_ids, 50):
        try:
            response = ssm_client.describe_instance_patch_states(InstanceIds=batch)
            for state in response.get("InstancePatchStates", []):
                states[state["InstanceId"]] = state
        except ClientError:
            for instance_id in batch:
                try:
                    response = ssm_client.describe_instance_patch_states(InstanceIds=[instance_id])
                    for state in response.get("InstancePatchStates", []):
                        states[state["InstanceId"]] = state
                except ClientError as error:
                    LOGGER.warning("failed to describe patch state for %s: %s", instance_id, error)
    return states


def fetch_instance_details(instance_ids: list[str]) -> dict[str, dict[str, Any]]:
    details: dict[str, dict[str, Any]] = {}
    for batch in chunks(instance_ids, 100):
        try:
            response = ec2_client.describe_instances(InstanceIds=batch)
        except ClientError as error:
            LOGGER.warning("failed to describe instances %s: %s", batch, error)
            continue

        for reservation in response.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
                instance_id = instance["InstanceId"]
                details[instance_id] = {
                    "instance_id": instance_id,
                    "hostname": tags.get("Name") or instance.get("PrivateDnsName") or instance_id,
                    "owner": tags.get("Owner"),
                    "environment": tags.get("Environment"),
                    "patch_management_enabled": tags.get(PATCH_MANAGEMENT_TAG_KEY) == PATCH_MANAGEMENT_TAG_VALUE,
                    "patch_reboot_window": tags.get(PATCH_REBOOT_WINDOW_TAG_KEY),
                    "has_reboot_required_tag": REBOOT_REQUIRED_TAG_KEY in tags,
                }
    return details


def process_instance(
    *,
    account_id: str,
    region: str,
    instance_id: str,
    now: datetime,
    patch_state: dict[str, Any] | None,
    instance_data: dict[str, Any] | None,
    active_request: dict[str, Any] | None,
) -> None:
    pending_reboot_count = int((patch_state or {}).get("InstalledPendingRebootCount", 0))

    if pending_reboot_count == 0:
        if active_request:
            if instance_data and instance_data.get("has_reboot_required_tag"):
                delete_reboot_required_tag(instance_id)
            update_request(
                active_request,
                {
                    "status": "RESOLVED",
                    "resolution_reason": "MANUAL_OR_EXTERNAL_REBOOT",
                    "updated_at": isoformat(now),
                },
            )
        return

    if not instance_data:
        if active_request:
            update_request(
                active_request,
                {
                    "status": "INSTANCE_NOT_FOUND",
                    "resolution_reason": "INSTANCE_NOT_FOUND",
                    "updated_at": isoformat(now),
                },
            )
        return

    if not instance_data["patch_management_enabled"]:
        return

    base_fields = build_request_metadata(
        account_id=account_id,
        region=region,
        instance_data=instance_data,
        pending_reboot_count=pending_reboot_count,
        now=now,
    )

    if not instance_data["patch_reboot_window"]:
        latest_request = active_request or fetch_latest_request(account_id, instance_id)
        if latest_request and latest_request.get("status") == "MANUAL":
            update_request(latest_request, {"status": "MANUAL", **base_fields})
        elif latest_request and latest_request.get("status") in ACTIVE_STATUSES:
            update_request(
                latest_request,
                {
                    **base_fields,
                    "status": "MANUAL",
                    "resolution_reason": "MISSING_PATCH_REBOOT_WINDOW",
                },
            )
        else:
            put_new_request(status="MANUAL", fields=base_fields, now=now)
        return

    if not active_request:
        put_new_request(status="PENDING_APPROVAL", fields=base_fields, now=now)
        return

    status = active_request["status"]
    if status == "POSTPONED":
        configuration_error = validate_postponed_request(active_request)
        if configuration_error:
            update_request(
                active_request,
                {
                    **base_fields,
                    "status": "FAILED_CONFIGURATION",
                    "resolution_reason": configuration_error,
                    "updated_at": isoformat(now),
                },
            )
            return

        postponed_until = parse_timestamp(active_request.get("postponed_until"))
        if postponed_until and now >= postponed_until:
            update_request(
                active_request,
                {
                    **base_fields,
                    "status": "AUTO_APPROVED",
                    "updated_at": isoformat(now),
                },
            )
        return

    if status == "TAGGED_FOR_REBOOT":
        grace_until = parse_timestamp(active_request.get("grace_until"))
        if not grace_until:
            update_request(
                active_request,
                {
                    **base_fields,
                    "status": "FAILED_CONFIGURATION",
                    "resolution_reason": "TAGGED_FOR_REBOOT_WITHOUT_GRACE_UNTIL",
                    "updated_at": isoformat(now),
                },
            )
            return
        if grace_until and now < grace_until:
            return

        update_request(
            active_request,
            {
                **base_fields,
                "status": "FAILED_REBOOT",
                "resolution_reason": "GRACE_PERIOD_EXPIRED_WITH_PENDING_REBOOT",
                "updated_at": isoformat(now),
            },
        )
        return

    if status in {"PENDING_APPROVAL", "APPROVED", "AUTO_APPROVED"}:
        update_request(active_request, base_fields)


def build_request_metadata(
    *,
    account_id: str,
    region: str,
    instance_data: dict[str, Any],
    pending_reboot_count: int,
    now: datetime,
) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "region": region,
        "instance_id": instance_data["instance_id"],
        "hostname": instance_data["hostname"],
        "owner": instance_data.get("owner"),
        "environment": instance_data.get("environment"),
        "patch_reboot_window": instance_data.get("patch_reboot_window"),
        "installed_pending_reboot_count": pending_reboot_count,
        "updated_at": isoformat(now),
    }


def fetch_latest_request(account_id: str, instance_id: str) -> dict[str, Any] | None:
    response = table.query(
        KeyConditionExpression=Key("pk").eq(build_pk(account_id, instance_id)),
        ScanIndexForward=False,
        Limit=1,
    )
    items = response.get("Items", [])
    return items[0] if items else None


def put_new_request(*, status: str, fields: dict[str, Any], now: datetime) -> None:
    request_id = str(uuid.uuid4())
    timestamp = isoformat(now)
    item = {
        "pk": build_pk(fields["account_id"], fields["instance_id"]),
        "sk": f"REBOOT#{timestamp}#{request_id}",
        "request_id": request_id,
        "status": status,
        "postpone_count": 0,
        "postponed_until": None,
        "approved_by": None,
        "approved_at": None,
        "tagged_for_reboot_at": None,
        "grace_until": None,
        "resolution_reason": None,
        "last_error": None,
        "created_at": timestamp,
        "updated_at": timestamp,
        "expires_at": ttl_epoch(now),
        **fields,
    }
    apply_active_index_fields(item, status)
    table.put_item(Item=item)


def update_request(item: dict[str, Any], changes: dict[str, Any]) -> None:
    next_item = {**item, **changes}
    status = next_item["status"]
    apply_active_index_fields(next_item, status)

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


def apply_active_index_fields(item: dict[str, Any], status: str) -> None:
    if status in ACTIVE_STATUSES:
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


def validate_postponed_request(item: dict[str, Any]) -> str | None:
    postpone_count = int(item.get("postpone_count", 0))
    postponed_until = item.get("postponed_until")

    if postpone_count < 1:
        return "POSTPONED_WITHOUT_COUNT"
    if postpone_count > MAX_POSTPONES:
        return "POSTPONE_LIMIT_EXCEEDED"
    if not postponed_until:
        return "POSTPONED_WITHOUT_DEADLINE"
    return None


def delete_reboot_required_tag(instance_id: str) -> None:
    ec2_client.delete_tags(
        Resources=[instance_id],
        Tags=[{"Key": REBOOT_REQUIRED_TAG_KEY}],
    )


def build_pk(account_id: str, instance_id: str) -> str:
    return f"ACCOUNT#{account_id}#INSTANCE#{instance_id}"


def build_active_gsi_pk(status: str) -> str:
    return f"ACTIVE#{status}"


def build_active_gsi_sk(*, account_id: str, region: str, instance_id: str, created_at: str) -> str:
    return (
        f"ACCOUNT#{account_id}"
        f"#REGION#{region}"
        f"#INSTANCE#{instance_id}"
        f"#CREATED#{created_at}"
    )


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
