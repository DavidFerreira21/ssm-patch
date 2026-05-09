import logging
import os
from datetime import UTC, datetime
from itertools import islice
from typing import Any

import boto3
from botocore.exceptions import ClientError

from discovery_dynamodb import fetch_active_requests
from discovery_workflow import ACTIVE_STATUSES, log_instance_action, process_instance


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(LOG_LEVEL)

PATCH_MANAGEMENT_TAG_KEY = os.environ["PATCH_MANAGEMENT_TAG_KEY"]
PATCH_MANAGEMENT_TAG_VALUE = os.environ["PATCH_MANAGEMENT_TAG_VALUE"]
PATCH_INSTALL_WINDOW_TAG_KEY = os.environ["PATCH_INSTALL_WINDOW_TAG_KEY"]
PATCH_INSTALL_APPROVED_TAG_KEY = os.environ["PATCH_INSTALL_APPROVED_TAG_KEY"]
PATCH_INSTALL_APPROVED_TAG_VALUE = os.environ["PATCH_INSTALL_APPROVED_TAG_VALUE"]

ssm_client = boto3.client("ssm")
ec2_client = boto3.client("ec2")
sts_client = boto3.client("sts")


###########################################
# Shared Helpers
###########################################

def utc_now() -> datetime:
    """Return the current time in UTC."""
    return datetime.now(UTC)


def chunks(items: list[str], size: int):
    """Yield fixed-size batches so AWS APIs can be called within service limits."""
    iterator = iter(items)
    while True:
        batch = list(islice(iterator, size))
        if not batch:
            break
        yield batch


###########################################
# Lambda Handler
###########################################

def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Run the discovery cycle for the current region and process each tracked instance."""
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

    try:
        discovered_instance_ids = fetch_non_compliant_instance_ids()
    except ClientError:
        LOGGER.exception("failed to fetch non-compliant instances region=%s", region)
        discovered_instance_ids = set()

    try:
        active_requests = fetch_active_requests(region, ACTIVE_STATUSES)
    except ClientError:
        LOGGER.exception("failed to fetch active requests region=%s", region)
        active_requests = {}

    tracked_instance_ids = sorted(discovered_instance_ids | set(active_requests.keys()))
    LOGGER.info(
        "discovery scope non_compliant_instances=%s active_requests=%s tracked_instances=%s",
        len(discovered_instance_ids),
        len(active_requests),
        len(tracked_instance_ids),
    )

    instance_details = fetch_instance_details(tracked_instance_ids)
    try:
        enrich_instance_details_with_install_windows(instance_details)
    except ClientError:
        LOGGER.exception("failed to enrich install window metadata region=%s", region)

    processed = 0
    failed = 0
    for instance_id in tracked_instance_ids:
        try:
            process_instance(
                account_id=account_id,
                region=region,
                instance_id=instance_id,
                now=now,
                is_non_compliant=instance_id in discovered_instance_ids,
                instance_data=instance_details.get(instance_id),
                active_request=active_requests.get(instance_id),
            )
            processed += 1
        except Exception:
            failed += 1
            LOGGER.exception(
                "failed to process instance instance_id=%s account_id=%s region=%s",
                instance_id,
                account_id,
                region,
            )
            log_instance_action(
                "INSTANCE_PROCESSING_FAILED",
                instance_id=instance_id,
                level=logging.ERROR,
                previous_status=active_requests.get(instance_id, {}).get("status"),
                reason="UNHANDLED_EXCEPTION",
            )

    result = {
        "processed_instances": processed,
        "failed_instances": failed,
        "non_compliant_instances": len(discovered_instance_ids),
        "tracked_active_requests": len(active_requests),
    }
    LOGGER.info("discovery finished result=%s", result)
    return result


###########################################
# Functions of SSM Compliance
###########################################

def fetch_non_compliant_instance_ids() -> set[str]:
    """List managed instances in SSM patch compliance that are currently NON_COMPLIANT."""
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


###########################################
# Functions of EC2 Inventory
###########################################

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
                    "patch_install_window": tags.get(PATCH_INSTALL_WINDOW_TAG_KEY),
                    "patch_install_window_description": None,
                    "next_install_window_at": None,
                    "has_patch_install_approved_tag": (
                        tags.get(PATCH_INSTALL_APPROVED_TAG_KEY) == PATCH_INSTALL_APPROVED_TAG_VALUE
                    ),
                }
    LOGGER.info("fetched instance details total=%s requested=%s", len(details), len(instance_ids))
    return details


###########################################
# Functions of Maintenance Windows
###########################################

def enrich_instance_details_with_install_windows(instance_details: dict[str, dict[str, Any]]) -> None:
    """Populate install window description and next execution time for each instance detail."""
    window_names = {
        details["patch_install_window"]
        for details in instance_details.values()
        if details.get("patch_install_window")
    }
    if not window_names:
        return

    windows_by_name = fetch_install_window_metadata(window_names)
    for details in instance_details.values():
        window_name = details.get("patch_install_window")
        if not window_name:
            continue
        window_data = windows_by_name.get(window_name)
        if not window_data:
            continue
        details["patch_install_window_description"] = window_data.get("description")
        details["next_install_window_at"] = window_data.get("next_execution_time")


def fetch_install_window_metadata(window_names: set[str]) -> dict[str, dict[str, str | None]]:
    """Load maintenance window metadata by window name and return human-friendly details."""
    results: dict[str, dict[str, str | None]] = {}
    identities: list[dict[str, Any]] = []
    next_token = None
    while True:
        request: dict[str, Any] = {"MaxResults": 50}
        if next_token:
            request["NextToken"] = next_token
        try:
            response = ssm_client.describe_maintenance_windows(**request)
        except ClientError as error:
            LOGGER.warning("failed to describe maintenance windows error=%s", error)
            return results
        identities.extend(response.get("WindowIdentities", []))
        next_token = response.get("NextToken")
        if not next_token:
            break

    for window_name in sorted(window_names):
        window = resolve_install_window(identities, window_name)
        if not window:
            continue

        schedule = window.get("Schedule")
        schedule_timezone = window.get("ScheduleTimezone")
        results[window_name] = {
            "description": humanize_maintenance_window_schedule(schedule, schedule_timezone),
            "next_execution_time": window.get("NextExecutionTime"),
        }
    return results


def resolve_install_window(
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
