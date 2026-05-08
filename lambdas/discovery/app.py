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
LOGGER = logging.getLogger(__name__)
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
REFRESHABLE_ACTIVE_STATUSES = {"PENDING_APPROVAL", "APPROVED", "AUTO_APPROVED"}

ssm_client = boto3.client("ssm")
ec2_client = boto3.client("ec2")
sts_client = boto3.client("sts")
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)


def utc_now() -> datetime:
    """Return the current time in UTC."""
    return datetime.now(UTC)


def isoformat(value: datetime) -> str:
    """Format a datetime in the timestamp shape used across DynamoDB items."""
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ttl_epoch(value: datetime) -> int:
    """Convert a datetime into the TTL epoch used to expire old requests."""
    return int((value + timedelta(days=RETENTION_DAYS)).timestamp())


def chunks(items: list[str], size: int):
    """Yield fixed-size batches so AWS APIs can be called within service limits."""
    iterator = iter(items)
    while True:
        batch = list(islice(iterator, size))
        if not batch:
            break
        yield batch


def log_instance_action(
    action: str,
    *,
    instance_id: str,
    level: int = logging.INFO,
    reason: str | None = None,
    previous_status: str | None = None,
    next_status: str | None = None,
    pending_reboot_count: int | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Write a structured log line that explains what discovery decided for one instance."""
    details: list[str] = [f"action={action}", f"instance_id={instance_id}"]
    if previous_status:
        details.append(f"previous_status={previous_status}")
    if next_status:
        details.append(f"next_status={next_status}")
    if reason:
        details.append(f"reason={reason}")
    if pending_reboot_count is not None:
        details.append(f"pending_reboot_count={pending_reboot_count}")
    if extra:
        for key in sorted(extra):
            details.append(f"{key}={extra[key]}")
    LOGGER.log(level, " ".join(details))


def summarize_metadata_changes(item: dict[str, Any], changes: dict[str, Any]) -> list[str]:
    """Summarize only the metadata fields that changed during a request refresh."""
    summary: list[str] = []
    tracked_fields = (
        "hostname",
        "owner",
        "environment",
        "patch_reboot_window",
        "patch_reboot_window_description",
        "next_reboot_window_at",
        "installed_pending_reboot_count",
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


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Run the discovery cycle for the current region and process each tracked instance."""
    # Discovery builds a single working set:
    # 1. instances currently non-compliant in SSM
    # 2. instances that already have an active request in DynamoDB
    account_id = sts_client.get_caller_identity()["Account"]
    region = os.environ["AWS_REGION"]
    now = utc_now()
    request_id = getattr(context, "aws_request_id", "unknown")

    LOGGER.info(
        "discovery started request_id=%s account_id=%s region=%s",
        request_id,
        account_id,
        region,
    )
    LOGGER.debug("discovery event=%s", event)

    discovered_instance_ids = fetch_non_compliant_instance_ids()
    active_requests = fetch_active_requests(region)
    tracked_instance_ids = sorted(discovered_instance_ids | set(active_requests.keys()))
    LOGGER.info(
        "discovery scope non_compliant_instances=%s active_requests=%s tracked_instances=%s",
        len(discovered_instance_ids),
        len(active_requests),
        len(tracked_instance_ids),
    )

    patch_states = fetch_patch_states(tracked_instance_ids)
    instance_details = fetch_instance_details(tracked_instance_ids)
    enrich_instance_details_with_reboot_windows(instance_details)

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

    result = {
        "processed_instances": processed,
        "non_compliant_instances": len(discovered_instance_ids),
        "tracked_active_requests": len(active_requests),
    }
    LOGGER.info("discovery finished result=%s", result)
    return result


def fetch_non_compliant_instance_ids() -> set[str]:
    """List managed instances in SSM patch compliance that are currently NON_COMPLIANT."""
    # SSM returns summaries for several resource types. We only care about managed instances.
    instance_ids: set[str] = set()
    next_token = None
    page = 0

    while True:
        page += 1
        request: dict[str, Any] = {
            "Filters": [
                {"Key": "ComplianceType", "Values": ["Patch"], "Type": "EQUAL"},
                {"Key": "Status", "Values": ["NON_COMPLIANT"], "Type": "EQUAL"},
            ]
        }
        if next_token:
            request["NextToken"] = next_token

        response = ssm_client.list_resource_compliance_summaries(**request)
        page_items = 0
        for item in response.get("ResourceComplianceSummaryItems", []):
            if item.get("ResourceType") != "ManagedInstance":
                continue
            resource_id = item.get("ResourceId")
            if resource_id:
                instance_ids.add(resource_id)
                page_items += 1

        LOGGER.debug(
            "fetched non-compliant compliance page=%s managed_instances=%s next_token=%s",
            page,
            page_items,
            bool(response.get("NextToken")),
        )

        next_token = response.get("NextToken")
        if not next_token:
            LOGGER.info("fetched non-compliant managed instances total=%s", len(instance_ids))
            return instance_ids


def fetch_active_requests(region: str) -> dict[str, dict[str, Any]]:
    """Load the newest active request per instance for the lambda's current region."""
    # The table is centralized, but each regional lambda must only work on its own requests.
    results: dict[str, dict[str, Any]] = {}

    for status in ACTIVE_STATUSES:
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

    LOGGER.info("fetched active request instances region=%s total=%s", region, len(results))
    return results


def fetch_patch_states(instance_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Load patch state details from SSM for each tracked instance."""
    states: dict[str, dict[str, Any]] = {}
    if not instance_ids:
        LOGGER.info("skipping patch state lookup because there are no tracked instances")
        return states

    for batch in chunks(instance_ids, 50):
        try:
            response = ssm_client.describe_instance_patch_states(InstanceIds=batch)
            for state in response.get("InstancePatchStates", []):
                states[state["InstanceId"]] = state
        except ClientError as error:
            # When one instance in the batch breaks the call, retry individually so the rest still move forward.
            LOGGER.warning("failed patch state batch lookup instance_ids=%s error=%s", batch, error)
            for instance_id in batch:
                try:
                    response = ssm_client.describe_instance_patch_states(InstanceIds=[instance_id])
                    for state in response.get("InstancePatchStates", []):
                        states[state["InstanceId"]] = state
                except ClientError as error:
                    LOGGER.warning("failed to describe patch state for %s: %s", instance_id, error)
    LOGGER.info("fetched patch states total=%s requested=%s", len(states), len(instance_ids))
    return states


def fetch_instance_details(instance_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Load EC2 metadata and relevant tags for each tracked instance."""
    details: dict[str, dict[str, Any]] = {}
    if not instance_ids:
        LOGGER.info("skipping instance detail lookup because there are no tracked instances")
        return details

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
                    "patch_reboot_window_description": None,
                    "next_reboot_window_at": None,
                    "has_reboot_required_tag": REBOOT_REQUIRED_TAG_KEY in tags,
                }
    LOGGER.info("fetched instance details total=%s requested=%s", len(details), len(instance_ids))
    return details


def enrich_instance_details_with_reboot_windows(instance_details: dict[str, dict[str, Any]]) -> None:
    """Populate reboot window description and next execution time for each instance detail."""
    window_names = {
        details["patch_reboot_window"]
        for details in instance_details.values()
        if details.get("patch_reboot_window")
    }
    if not window_names:
        return

    windows_by_name = fetch_reboot_window_metadata(window_names)
    for details in instance_details.values():
        window_name = details.get("patch_reboot_window")
        if not window_name:
            continue
        window_data = windows_by_name.get(window_name)
        if not window_data:
            continue
        details["patch_reboot_window_description"] = window_data.get("description")
        details["next_reboot_window_at"] = window_data.get("next_execution_time")


def fetch_reboot_window_metadata(window_names: set[str]) -> dict[str, dict[str, str | None]]:
    """Load maintenance window metadata by window name and return human-friendly details."""
    results: dict[str, dict[str, str | None]] = {}
    for window_name in sorted(window_names):
        try:
            response = ssm_client.describe_maintenance_windows(MaxResults=50)
        except ClientError as error:
            LOGGER.warning("failed to describe maintenance window name=%s error=%s", window_name, error)
            continue

        identities = response.get("WindowIdentities", [])
        window = resolve_maintenance_window(identities, window_name)
        if not window:
            continue

        schedule = window.get("Schedule")
        schedule_timezone = window.get("ScheduleTimezone")
        results[window_name] = {
            "description": humanize_maintenance_window_schedule(schedule, schedule_timezone),
            "next_execution_time": window.get("NextExecutionTime"),
        }
    return results


def resolve_maintenance_window(
    identities: list[dict[str, Any]],
    window_name_prefix: str,
) -> dict[str, Any] | None:
    """Resolve a maintenance window by exact name first, then by prefix match."""
    exact_match = next((item for item in identities if item.get("Name") == window_name_prefix), None)
    if exact_match:
        return exact_match

    prefix_matches = [
        item
        for item in identities
        if (item.get("Name") or "").startswith(window_name_prefix)
    ]
    if len(prefix_matches) == 1:
        return prefix_matches[0]

    if len(prefix_matches) > 1:
        LOGGER.warning(
            "maintenance window prefix is ambiguous prefix=%s matches=%s",
            window_name_prefix,
            [item.get("Name") for item in prefix_matches],
        )
        return None

    LOGGER.warning("maintenance window not found for prefix=%s", window_name_prefix)
    return None


def humanize_maintenance_window_schedule(schedule: str | None, schedule_timezone: str | None) -> str | None:
    """Convert a maintenance window cron expression into a short human-friendly description."""
    if not schedule:
        return None

    if not schedule.startswith("cron(") or not schedule.endswith(")"):
        return schedule

    parts = schedule[5:-1].split()
    if len(parts) != 6:
        return schedule

    minute, hour, day_of_month, month, day_of_week, _year = parts
    timezone_text = f" ({schedule_timezone})" if schedule_timezone else ""

    if minute.startswith("0/") and hour == "*" and day_of_month == "*" and day_of_week == "?":
        interval = minute.split("/", 1)[1]
        return f"a cada {interval} minutos{timezone_text}"

    if minute == "0" and hour.startswith("0/") and day_of_month == "*" and day_of_week == "?":
        interval = hour.split("/", 1)[1]
        return f"a cada {interval} horas{timezone_text}"

    if not hour.isdigit() or not minute.isdigit():
        return schedule

    time_text = f"{hour.zfill(2)}:{minute.zfill(2)}"

    if day_of_month == "*" and day_of_week == "?":
        return f"todos os dias às {time_text}{timezone_text}"

    if day_of_month == "?" and day_of_week not in {"*", "?"}:
        days_text = humanize_days_of_week(day_of_week)
        return f"{days_text} às {time_text}{timezone_text}"

    if day_of_month not in {"*", "?"} and day_of_week == "?":
        return f"dia {day_of_month} de cada mês às {time_text}{timezone_text}"

    return schedule


def humanize_days_of_week(day_of_week: str) -> str:
    """Convert AWS cron day-of-week values into Portuguese text."""
    day_map = {
        "SUN": "domingo",
        "MON": "segunda",
        "TUE": "terça",
        "WED": "quarta",
        "THU": "quinta",
        "FRI": "sexta",
        "SAT": "sábado",
    }
    day_parts = [part.strip() for part in day_of_week.split(",") if part.strip()]
    translated = [day_map.get(part, part) for part in day_parts]
    if len(translated) == 1:
        return translated[0]
    if len(translated) == 2:
        return f"{translated[0]} e {translated[1]}"
    return ", ".join(translated[:-1]) + f" e {translated[-1]}"


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
    """Route one instance through the discovery decision tree based on its current state."""
    pending_reboot_count = int((patch_state or {}).get("InstalledPendingRebootCount", 0))
    active_status = active_request.get("status") if active_request else None
    log_instance_action(
        "EVALUATE",
        instance_id=instance_id,
        level=logging.DEBUG,
        previous_status=active_status,
        pending_reboot_count=pending_reboot_count,
        extra={"has_instance_data": bool(instance_data)},
    )

    if pending_reboot_count == 0:
        handle_instance_without_pending_reboot(
            instance_id=instance_id,
            now=now,
            instance_data=instance_data,
            active_request=active_request,
            pending_reboot_count=pending_reboot_count,
        )
        return

    if not instance_data:
        handle_instance_without_details(
            instance_id=instance_id,
            now=now,
            active_request=active_request,
            pending_reboot_count=pending_reboot_count,
        )
        return

    if not instance_data["patch_management_enabled"]:
        handle_patch_management_disabled(
            instance_id=instance_id,
            active_request=active_request,
            pending_reboot_count=pending_reboot_count,
        )
        return

    base_fields = build_request_metadata(
        account_id=account_id,
        region=region,
        instance_data=instance_data,
        pending_reboot_count=pending_reboot_count,
        now=now,
    )

    if not instance_data["patch_reboot_window"]:
        handle_missing_reboot_window(
            account_id=account_id,
            instance_id=instance_id,
            now=now,
            base_fields=base_fields,
            active_request=active_request,
            pending_reboot_count=pending_reboot_count,
        )
        return

    if not active_request:
        create_pending_approval_request(
            instance_id=instance_id,
            now=now,
            base_fields=base_fields,
            pending_reboot_count=pending_reboot_count,
        )
        return

    status = active_request["status"]
    if status == "POSTPONED":
        handle_postponed_request(
            instance_id=instance_id,
            now=now,
            base_fields=base_fields,
            active_request=active_request,
            pending_reboot_count=pending_reboot_count,
        )
        return

    if status == "TAGGED_FOR_REBOOT":
        handle_tagged_for_reboot_request(
            instance_id=instance_id,
            now=now,
            base_fields=base_fields,
            active_request=active_request,
            pending_reboot_count=pending_reboot_count,
        )
        return

    if status in REFRESHABLE_ACTIVE_STATUSES:
        refresh_active_request(
            instance_id=instance_id,
            base_fields=base_fields,
            active_request=active_request,
            pending_reboot_count=pending_reboot_count,
        )


def handle_instance_without_pending_reboot(
    *,
    instance_id: str,
    now: datetime,
    instance_data: dict[str, Any] | None,
    active_request: dict[str, Any] | None,
    pending_reboot_count: int,
) -> None:
    """Resolve an active request when the instance no longer reports pending reboot."""
    # No pending reboot means any active request can be closed out.
    if not active_request:
        log_instance_action(
            "SKIP",
            instance_id=instance_id,
            level=logging.DEBUG,
            reason="NO_PENDING_REBOOT_AND_NO_ACTIVE_REQUEST",
            pending_reboot_count=pending_reboot_count,
        )
        return

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
    log_instance_action(
        "RESOLVE_REQUEST",
        instance_id=instance_id,
        previous_status=active_request.get("status"),
        next_status="RESOLVED",
        reason="MANUAL_OR_EXTERNAL_REBOOT",
    )


def handle_instance_without_details(
    *,
    instance_id: str,
    now: datetime,
    active_request: dict[str, Any] | None,
    pending_reboot_count: int,
) -> None:
    """Handle instances that no longer appear in EC2 while a request is still active."""
    # The request still exists, but the instance is no longer returned by EC2.
    if not active_request:
        log_instance_action(
            "SKIP",
            instance_id=instance_id,
            level=logging.DEBUG,
            reason="INSTANCE_DETAILS_MISSING_WITHOUT_ACTIVE_REQUEST",
            pending_reboot_count=pending_reboot_count,
        )
        return

    update_request(
        active_request,
        {
            "status": "INSTANCE_NOT_FOUND",
            "resolution_reason": "INSTANCE_NOT_FOUND",
            "updated_at": isoformat(now),
        },
    )
    log_instance_action(
        "UPDATE_REQUEST",
        instance_id=instance_id,
        level=logging.WARNING,
        previous_status=active_request.get("status"),
        next_status="INSTANCE_NOT_FOUND",
        reason="INSTANCE_DETAILS_MISSING",
        pending_reboot_count=pending_reboot_count,
    )


def handle_patch_management_disabled(
    *,
    instance_id: str,
    active_request: dict[str, Any] | None,
    pending_reboot_count: int,
) -> None:
    """Skip instances that are no longer in the PatchManagement scope."""
    # Out-of-scope instances are ignored here; another workflow can decide whether they should be cancelled.
    log_instance_action(
        "SKIP",
        instance_id=instance_id,
        level=logging.INFO,
        previous_status=active_request.get("status") if active_request else None,
        reason="PATCH_MANAGEMENT_DISABLED",
        pending_reboot_count=pending_reboot_count,
    )


def handle_missing_reboot_window(
    *,
    account_id: str,
    instance_id: str,
    now: datetime,
    base_fields: dict[str, Any],
    active_request: dict[str, Any] | None,
    pending_reboot_count: int,
) -> None:
    """Move the request to manual handling when the reboot window tag is missing."""
    # Missing reboot window means the automation cannot continue safely.
    latest_request = active_request or fetch_latest_request(account_id, base_fields["region"], instance_id)
    if latest_request and latest_request.get("status") == "MANUAL":
        update_request(latest_request, {"status": "MANUAL", **base_fields})
        log_instance_action(
            "REFRESH_REQUEST",
            instance_id=instance_id,
            previous_status="MANUAL",
            next_status="MANUAL",
            reason="STATUS_UNCHANGED_METADATA_REFRESH",
            pending_reboot_count=pending_reboot_count,
        )
        return

    if latest_request and latest_request.get("status") in ACTIVE_STATUSES:
        update_request(
            latest_request,
            {
                **base_fields,
                "status": "MANUAL",
                "resolution_reason": "MISSING_PATCH_REBOOT_WINDOW",
            },
        )
        log_instance_action(
            "UPDATE_REQUEST",
            instance_id=instance_id,
            level=logging.WARNING,
            previous_status=latest_request.get("status"),
            next_status="MANUAL",
            reason="MISSING_PATCH_REBOOT_WINDOW",
            pending_reboot_count=pending_reboot_count,
        )
        return

    put_new_request(status="MANUAL", fields=base_fields, now=now)
    log_instance_action(
        "CREATE_REQUEST",
        instance_id=instance_id,
        next_status="MANUAL",
        reason="MISSING_PATCH_REBOOT_WINDOW",
        pending_reboot_count=pending_reboot_count,
    )


def create_pending_approval_request(
    *,
    instance_id: str,
    now: datetime,
    base_fields: dict[str, Any],
    pending_reboot_count: int,
) -> None:
    """Create the first approval request for a newly discovered pending reboot."""
    # First time we discover this managed instance with pending reboot.
    put_new_request(status="PENDING_APPROVAL", fields=base_fields, now=now)
    log_instance_action(
        "CREATE_REQUEST",
        instance_id=instance_id,
        next_status="PENDING_APPROVAL",
        reason="NON_COMPLIANT_PENDING_REBOOT",
        pending_reboot_count=pending_reboot_count,
    )


def handle_postponed_request(
    *,
    instance_id: str,
    now: datetime,
    base_fields: dict[str, Any],
    active_request: dict[str, Any],
    pending_reboot_count: int,
) -> None:
    """Validate, wait, or auto-approve a postponed request based on its deadline."""
    # POSTPONED can either wait, auto-approve after the deadline, or fail configuration validation.
    status = active_request["status"]
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
        log_instance_action(
            "UPDATE_REQUEST",
            instance_id=instance_id,
            level=logging.WARNING,
            previous_status=status,
            next_status="FAILED_CONFIGURATION",
            reason=configuration_error,
            pending_reboot_count=pending_reboot_count,
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
        log_instance_action(
            "UPDATE_REQUEST",
            instance_id=instance_id,
            previous_status=status,
            next_status="AUTO_APPROVED",
            reason="POSTPONEMENT_EXPIRED",
            pending_reboot_count=pending_reboot_count,
        )
        return

    log_instance_action(
        "SKIP",
        instance_id=instance_id,
        level=logging.DEBUG,
        previous_status=status,
        reason="POSTPONEMENT_STILL_ACTIVE",
        pending_reboot_count=pending_reboot_count,
        extra={"postponed_until": active_request.get("postponed_until")},
    )


def handle_tagged_for_reboot_request(
    *,
    instance_id: str,
    now: datetime,
    base_fields: dict[str, Any],
    active_request: dict[str, Any],
    pending_reboot_count: int,
) -> None:
    """Watch the executor grace period and fail the request if reboot never clears."""
    # After the executor has tagged the instance, discovery only watches the grace period.
    status = active_request["status"]
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
        log_instance_action(
            "UPDATE_REQUEST",
            instance_id=instance_id,
            level=logging.WARNING,
            previous_status=status,
            next_status="FAILED_CONFIGURATION",
            reason="TAGGED_FOR_REBOOT_WITHOUT_GRACE_UNTIL",
            pending_reboot_count=pending_reboot_count,
        )
        return

    if now < grace_until:
        log_instance_action(
            "SKIP",
            instance_id=instance_id,
            level=logging.DEBUG,
            previous_status=status,
            reason="GRACE_PERIOD_STILL_ACTIVE",
            pending_reboot_count=pending_reboot_count,
            extra={"grace_until": active_request.get("grace_until")},
        )
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
    log_instance_action(
        "UPDATE_REQUEST",
        instance_id=instance_id,
        level=logging.WARNING,
        previous_status=status,
        next_status="FAILED_REBOOT",
        reason="GRACE_PERIOD_EXPIRED_WITH_PENDING_REBOOT",
        pending_reboot_count=pending_reboot_count,
    )


def refresh_active_request(
    *,
    instance_id: str,
    base_fields: dict[str, Any],
    active_request: dict[str, Any],
    pending_reboot_count: int,
) -> None:
    """Refresh metadata for active requests whose workflow status should not change."""
    # These statuses keep the same workflow state; discovery only refreshes the metadata snapshot.
    status = active_request["status"]
    update_request(active_request, base_fields)
    log_instance_action(
        "REFRESH_REQUEST",
        instance_id=instance_id,
        previous_status=status,
        next_status=status,
        reason="STATUS_UNCHANGED_METADATA_REFRESH",
        pending_reboot_count=pending_reboot_count,
    )


def build_request_metadata(
    *,
    account_id: str,
    region: str,
    instance_data: dict[str, Any],
    pending_reboot_count: int,
    now: datetime,
) -> dict[str, Any]:
    """Build the metadata snapshot stored with each request in DynamoDB."""
    # This is the snapshot discovery keeps in DynamoDB so approvers see the latest instance context.
    return {
        "account_id": account_id,
        "region": region,
        "instance_id": instance_data["instance_id"],
        "hostname": instance_data["hostname"],
        "owner": instance_data.get("owner"),
        "environment": instance_data.get("environment"),
        "patch_reboot_window": instance_data.get("patch_reboot_window"),
        "patch_reboot_window_description": instance_data.get("patch_reboot_window_description"),
        "next_reboot_window_at": instance_data.get("next_reboot_window_at"),
        "installed_pending_reboot_count": pending_reboot_count,
        "updated_at": isoformat(now),
    }


def fetch_latest_request(account_id: str, region: str, instance_id: str) -> dict[str, Any] | None:
    """Fetch the newest request row for one instance, regardless of current status."""
    response = table.query(
        KeyConditionExpression=Key("pk").eq(build_pk(account_id, region, instance_id)),
        ScanIndexForward=False,
        Limit=1,
    )
    items = response.get("Items", [])
    LOGGER.debug("fetched latest request instance_id=%s found=%s", instance_id, bool(items))
    return items[0] if items else None


def put_new_request(*, status: str, fields: dict[str, Any], now: datetime) -> None:
    """Insert a brand-new request row into DynamoDB with its initial workflow state."""
    request_id = str(uuid.uuid4())
    timestamp = isoformat(now)
    item = {
        "pk": build_pk(fields["account_id"], fields["region"], fields["instance_id"]),
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
    LOGGER.info(
        "created request request_id=%s instance_id=%s status=%s",
        request_id,
        fields["instance_id"],
        status,
    )


def update_request(item: dict[str, Any], changes: dict[str, Any]) -> None:
    """Apply a partial update to an existing request and keep the active-request GSI in sync."""
    next_item = {**item, **changes}
    status = next_item["status"]
    apply_active_index_fields(next_item, status)
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

    # Writes are partial on purpose: we keep the original PK/SK and only touch fields relevant to this transition.
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


def apply_active_index_fields(item: dict[str, Any], status: str) -> None:
    """Populate or clear the active-request GSI fields based on the request status."""
    # The GSI is only populated while the request is still actionable.
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
    """Return a configuration error code when a postponed request is inconsistent."""
    # POSTPONED is valid only when both the count and deadline are coherent.
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
    """Remove the reboot-required tag after the pending reboot has been cleared."""
    ec2_client.delete_tags(
        Resources=[instance_id],
        Tags=[{"Key": REBOOT_REQUIRED_TAG_KEY}],
    )
    LOGGER.info("deleted reboot required tag instance_id=%s", instance_id)


def build_pk(account_id: str, region: str, instance_id: str) -> str:
    """Build the partition key used to group all request rows for one instance in one region."""
    return f"ACCOUNT#{account_id}#REGION#{region}#INSTANCE#{instance_id}"


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


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse an ISO timestamp from DynamoDB into a timezone-aware datetime."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
