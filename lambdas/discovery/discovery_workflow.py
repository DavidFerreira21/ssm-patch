import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from botocore.exceptions import ClientError

from discovery_constants import (
    ACTIVE_STATUSES,
    REASON_FAILED_TO_REMOVE_PATCH_INSTALL_APPROVAL_TAG,
    REASON_INSTANCE_COMPLIANT,
    REASON_INSTANCE_COMPLIANT_APPROVAL_TAG_REMOVED,
    REASON_INSTANCE_COMPLIANT_WITHOUT_ACTIVE_REQUEST,
    REASON_INSTANCE_DETAILS_MISSING,
    REASON_INSTANCE_DETAILS_MISSING_WITHOUT_ACTIVE_REQUEST,
    REASON_INSTANCE_NOT_FOUND,
    REASON_INSTALL_DID_NOT_REMEDIATE_COMPLIANCE,
    REASON_INSTALL_GRACE_PERIOD_ACTIVE,
    REASON_INSTALL_WINDOW_NOT_REACHED,
    REASON_MISSING_PATCH_INSTALL_WINDOW,
    REASON_NON_COMPLIANT_REQUIRES_INSTALL_APPROVAL,
    REASON_PATCH_MANAGEMENT_DISABLED,
    REASON_POSTPONED_WITHOUT_COUNT,
    REASON_POSTPONED_WITHOUT_DEADLINE,
    REASON_POSTPONE_LIMIT_EXCEEDED,
    REASON_POSTPONEMENT_EXPIRED,
    REASON_POSTPONEMENT_STILL_ACTIVE,
    REASON_STATUS_UNCHANGED_METADATA_REFRESH,
    REASON_UNSUPPORTED_ACTIVE_STATUS,
    REFRESHABLE_ACTIVE_STATUSES,
    STATUS_AUTO_APPROVED,
    STATUS_FAILED_CONFIGURATION,
    STATUS_FAILED_REMEDIATION,
    STATUS_INSTANCE_NOT_FOUND,
    STATUS_MANUAL,
    STATUS_PENDING_APPROVAL,
    STATUS_POSTPONED,
    STATUS_RESOLVED,
    STATUS_INSTALL_READY,
    STATUS_AUTO_INSTALL_READY,
)
from discovery_dynamodb import fetch_latest_request, put_new_request, update_request

LOGGER = logging.getLogger(__name__)

PATCH_INSTALL_APPROVED_TAG_KEY = os.environ["PATCH_INSTALL_APPROVED_TAG_KEY"]
INSTALL_GRACE_HOURS = float(os.getenv("INSTALL_GRACE_HOURS", "8"))
MAX_POSTPONES = int(os.getenv("MAX_POSTPONES", "1"))

ec2_client = boto3.client("ec2")


def isoformat(value: datetime) -> str:
    """Format a datetime in the timestamp shape used across DynamoDB items."""
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def log_instance_action(
    action: str,
    *,
    instance_id: str,
    level: int = logging.INFO,
    reason: str | None = None,
    previous_status: str | None = None,
    next_status: str | None = None,
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
    if extra:
        for key in sorted(extra):
            details.append(f"{key}={extra[key]}")
    LOGGER.log(level, " ".join(details))


def process_instance(
    *,
    account_id: str,
    region: str,
    instance_id: str,
    now: datetime,
    is_non_compliant: bool,
    instance_data: dict[str, Any] | None,
    active_request: dict[str, Any] | None,
) -> None:
    """Route one instance through the discovery decision tree based on compliance and approval state."""
    log_instance_action(
        "EVALUATE",
        instance_id=instance_id,
        level=logging.DEBUG,
        previous_status=active_request.get("status") if active_request else None,
        extra={
            "has_active_request": bool(active_request),
            "has_instance_data": bool(instance_data),
            "is_non_compliant": is_non_compliant,
        },
    )

    if not instance_data:
        handle_instance_without_details(
            instance_id=instance_id,
            now=now,
            active_request=active_request,
        )
        return

    if not instance_data["patch_management_enabled"]:
        handle_patch_management_disabled(
            instance_id=instance_id,
            active_request=active_request,
        )
        return

    base_fields = build_request_metadata(
        account_id=account_id,
        region=region,
        instance_data=instance_data,
        now=now,
    )

    if not is_non_compliant:
        handle_compliant_instance(
            instance_id=instance_id,
            now=now,
            base_fields=base_fields,
            instance_data=instance_data,
            active_request=active_request,
        )
        return

    if not instance_data["patch_install_window"]:
        handle_missing_install_window(
            account_id=account_id,
            instance_id=instance_id,
            now=now,
            base_fields=base_fields,
            active_request=active_request,
        )
        return

    if not active_request:
        create_pending_approval_request(
            instance_id=instance_id,
            now=now,
            base_fields=base_fields,
        )
        return

    status = active_request["status"]
    if status == STATUS_POSTPONED:
        handle_postponed_request(
            instance_id=instance_id,
            now=now,
            base_fields=base_fields,
            active_request=active_request,
        )
        return

    if status in {STATUS_INSTALL_READY, STATUS_AUTO_INSTALL_READY}:
        handle_ready_for_install_request(
            instance_id=instance_id,
            now=now,
            base_fields=base_fields,
            active_request=active_request,
        )
        return

    if status in REFRESHABLE_ACTIVE_STATUSES:
        refresh_active_request(
            instance_id=instance_id,
            base_fields=base_fields,
            active_request=active_request,
        )
        return

    log_instance_action(
        "SKIP",
        instance_id=instance_id,
        previous_status=status,
        reason=REASON_UNSUPPORTED_ACTIVE_STATUS,
    )


def handle_compliant_instance(
    *,
    instance_id: str,
    now: datetime,
    base_fields: dict[str, Any],
    instance_data: dict[str, Any],
    active_request: dict[str, Any] | None,
) -> None:
    """Resolve the request and clean the install-approval tag once the instance is compliant."""
    if instance_data.get("has_patch_install_approved_tag"):
        try:
            delete_patch_install_approved_tag(instance_id)
            log_instance_action(
                "CLEANUP_TAG",
                instance_id=instance_id,
                previous_status=(
                    active_request.get("status") if active_request else None
                ),
                reason=REASON_INSTANCE_COMPLIANT_APPROVAL_TAG_REMOVED,
            )
        except ClientError as error:
            LOGGER.warning(
                "failed to delete patch install approved tag instance_id=%s error=%s",
                instance_id,
                error,
            )
            log_instance_action(
                "CLEANUP_TAG_FAILED",
                instance_id=instance_id,
                level=logging.WARNING,
                previous_status=(
                    active_request.get("status") if active_request else None
                ),
                reason=REASON_FAILED_TO_REMOVE_PATCH_INSTALL_APPROVAL_TAG,
                extra={"error": str(error)},
            )
            return

    if not active_request:
        log_instance_action(
            "SKIP",
            instance_id=instance_id,
            level=logging.DEBUG,
            reason=REASON_INSTANCE_COMPLIANT_WITHOUT_ACTIVE_REQUEST,
        )
        return

    update_request(
        active_request,
        {
            **base_fields,
            "status": STATUS_RESOLVED,
            "resolution_reason": REASON_INSTANCE_COMPLIANT,
            "updated_at": isoformat(now),
        },
        ACTIVE_STATUSES,
    )
    log_instance_action(
        "RESOLVE_REQUEST",
        instance_id=instance_id,
        previous_status=active_request.get("status"),
        next_status=STATUS_RESOLVED,
        reason=REASON_INSTANCE_COMPLIANT,
    )


def handle_instance_without_details(
    *,
    instance_id: str,
    now: datetime,
    active_request: dict[str, Any] | None,
) -> None:
    """Handle instances that no longer appear in EC2 while a request is still active."""
    if not active_request:
        log_instance_action(
            "SKIP",
            instance_id=instance_id,
            level=logging.DEBUG,
            reason=REASON_INSTANCE_DETAILS_MISSING_WITHOUT_ACTIVE_REQUEST,
        )
        return

    update_request(
        active_request,
        {
            "status": STATUS_INSTANCE_NOT_FOUND,
            "resolution_reason": REASON_INSTANCE_NOT_FOUND,
            "updated_at": isoformat(now),
        },
        ACTIVE_STATUSES,
    )
    log_instance_action(
        "UPDATE_REQUEST",
        instance_id=instance_id,
        level=logging.WARNING,
        previous_status=active_request.get("status"),
        next_status=STATUS_INSTANCE_NOT_FOUND,
        reason=REASON_INSTANCE_DETAILS_MISSING,
    )


def handle_patch_management_disabled(
    *,
    instance_id: str,
    active_request: dict[str, Any] | None,
) -> None:
    """Skip instances that are no longer in the PatchManagement scope."""
    log_instance_action(
        "SKIP",
        instance_id=instance_id,
        level=logging.INFO,
        previous_status=active_request.get("status") if active_request else None,
        reason=REASON_PATCH_MANAGEMENT_DISABLED,
    )


def handle_missing_install_window(
    *,
    account_id: str,
    instance_id: str,
    now: datetime,
    base_fields: dict[str, Any],
    active_request: dict[str, Any] | None,
) -> None:
    """Move the request to manual handling when the install window tag is missing."""
    latest_request = active_request or fetch_latest_request(
        account_id, base_fields["region"], instance_id
    )
    if latest_request and latest_request.get("status") == STATUS_MANUAL:
        update_request(
            latest_request, {"status": STATUS_MANUAL, **base_fields}, ACTIVE_STATUSES
        )
        log_instance_action(
            "REFRESH_REQUEST",
            instance_id=instance_id,
            previous_status=STATUS_MANUAL,
            next_status=STATUS_MANUAL,
            reason=REASON_STATUS_UNCHANGED_METADATA_REFRESH,
        )
        return

    if latest_request and latest_request.get("status") in ACTIVE_STATUSES:
        update_request(
            latest_request,
            {
                **base_fields,
                "status": STATUS_MANUAL,
                "resolution_reason": REASON_MISSING_PATCH_INSTALL_WINDOW,
            },
            ACTIVE_STATUSES,
        )
        log_instance_action(
            "UPDATE_REQUEST",
            instance_id=instance_id,
            level=logging.WARNING,
            previous_status=latest_request.get("status"),
            next_status=STATUS_MANUAL,
            reason=REASON_MISSING_PATCH_INSTALL_WINDOW,
        )
        return

    put_new_request(
        status=STATUS_MANUAL,
        fields=base_fields,
        now=now,
        active_statuses=ACTIVE_STATUSES,
    )
    log_instance_action(
        "CREATE_REQUEST",
        instance_id=instance_id,
        next_status=STATUS_MANUAL,
        reason=REASON_MISSING_PATCH_INSTALL_WINDOW,
    )


def create_pending_approval_request(
    *,
    instance_id: str,
    now: datetime,
    base_fields: dict[str, Any],
) -> None:
    """Create the first approval request for a newly discovered non-compliant instance."""
    put_new_request(
        status=STATUS_PENDING_APPROVAL,
        fields=base_fields,
        now=now,
        active_statuses=ACTIVE_STATUSES,
    )
    log_instance_action(
        "CREATE_REQUEST",
        instance_id=instance_id,
        next_status=STATUS_PENDING_APPROVAL,
        reason=REASON_NON_COMPLIANT_REQUIRES_INSTALL_APPROVAL,
    )


def handle_postponed_request(
    *,
    instance_id: str,
    now: datetime,
    base_fields: dict[str, Any],
    active_request: dict[str, Any],
) -> None:
    """Validate, wait, or auto-approve a postponed request based on its deadline."""
    status = active_request["status"]
    configuration_error = validate_postponed_request(active_request)
    if configuration_error:
        update_request(
            active_request,
            {
                **base_fields,
                "status": STATUS_FAILED_CONFIGURATION,
                "resolution_reason": configuration_error,
                "updated_at": isoformat(now),
            },
            ACTIVE_STATUSES,
        )
        log_instance_action(
            "UPDATE_REQUEST",
            instance_id=instance_id,
            level=logging.WARNING,
            previous_status=status,
            next_status=STATUS_FAILED_CONFIGURATION,
            reason=configuration_error,
        )
        return

    postponed_until = parse_timestamp(active_request.get("postponed_until"))
    if postponed_until and now >= postponed_until:
        update_request(
            active_request,
            {
                **base_fields,
                "status": STATUS_AUTO_APPROVED,
                "updated_at": isoformat(now),
            },
            ACTIVE_STATUSES,
        )
        log_instance_action(
            "UPDATE_REQUEST",
            instance_id=instance_id,
            previous_status=status,
            next_status=STATUS_AUTO_APPROVED,
            reason=REASON_POSTPONEMENT_EXPIRED,
        )
        return

    log_instance_action(
        "SKIP",
        instance_id=instance_id,
        level=logging.DEBUG,
        previous_status=status,
        reason=REASON_POSTPONEMENT_STILL_ACTIVE,
        extra={"postponed_until": active_request.get("postponed_until")},
    )


def refresh_active_request(
    *,
    instance_id: str,
    base_fields: dict[str, Any],
    active_request: dict[str, Any],
) -> None:
    """Refresh metadata for active requests whose workflow status should not change."""
    status = active_request["status"]
    update_request(active_request, base_fields, ACTIVE_STATUSES)
    log_instance_action(
        "REFRESH_REQUEST",
        instance_id=instance_id,
        previous_status=status,
        next_status=status,
        reason=REASON_STATUS_UNCHANGED_METADATA_REFRESH,
    )


def handle_ready_for_install_request(
    *,
    instance_id: str,
    now: datetime,
    base_fields: dict[str, Any],
    active_request: dict[str, Any],
) -> None:
    """Wait for the expected install window or fail the request if remediation did not clear compliance."""
    status = active_request["status"]
    expected_install_window_at = parse_timestamp(
        active_request.get("expected_install_window_at")
    )
    install_grace_until = parse_timestamp(active_request.get("install_grace_until"))

    if expected_install_window_at and now < expected_install_window_at:
        refresh_active_request(
            instance_id=instance_id,
            base_fields=base_fields,
            active_request=active_request,
        )
        log_instance_action(
            "SKIP",
            instance_id=instance_id,
            level=logging.DEBUG,
            previous_status=status,
            reason=REASON_INSTALL_WINDOW_NOT_REACHED,
            extra={
                "expected_install_window_at": active_request.get(
                    "expected_install_window_at"
                )
            },
        )
        return

    if install_grace_until and now < install_grace_until:
        refresh_active_request(
            instance_id=instance_id,
            base_fields=base_fields,
            active_request=active_request,
        )
        log_instance_action(
            "SKIP",
            instance_id=instance_id,
            level=logging.DEBUG,
            previous_status=status,
            reason=REASON_INSTALL_GRACE_PERIOD_ACTIVE,
            extra={"install_grace_until": active_request.get("install_grace_until")},
        )
        return

    update_request(
        active_request,
        {
            **base_fields,
            "status": STATUS_FAILED_REMEDIATION,
            "resolution_reason": REASON_INSTALL_DID_NOT_REMEDIATE_COMPLIANCE,
            "updated_at": isoformat(now),
        },
        ACTIVE_STATUSES,
    )
    log_instance_action(
        "UPDATE_REQUEST",
        instance_id=instance_id,
        level=logging.WARNING,
        previous_status=status,
        next_status=STATUS_FAILED_REMEDIATION,
        reason=REASON_INSTALL_DID_NOT_REMEDIATE_COMPLIANCE,
    )


def build_request_metadata(
    *,
    account_id: str,
    region: str,
    instance_data: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    """Build the metadata snapshot stored with each request in DynamoDB."""
    return {
        "account_id": account_id,
        "region": region,
        "instance_id": instance_data["instance_id"],
        "hostname": instance_data["hostname"],
        "owner": instance_data.get("owner"),
        "environment": instance_data.get("environment"),
        "patch_install_window": instance_data.get("patch_install_window"),
        "patch_install_window_description": instance_data.get(
            "patch_install_window_description"
        ),
        "next_install_window_at": instance_data.get("next_install_window_at"),
        "updated_at": isoformat(now),
    }


def validate_postponed_request(item: dict[str, Any]) -> str | None:
    """Return a configuration error code when a postponed request is inconsistent."""
    postpone_count = int(item.get("postpone_count", 0))
    postponed_until = item.get("postponed_until")

    if postpone_count < 1:
        return REASON_POSTPONED_WITHOUT_COUNT
    if postpone_count > MAX_POSTPONES:
        return REASON_POSTPONE_LIMIT_EXCEEDED
    if not postponed_until:
        return REASON_POSTPONED_WITHOUT_DEADLINE
    return None


def delete_patch_install_approved_tag(instance_id: str) -> None:
    """Remove the install-approval tag once the instance is compliant or already remediated."""
    ec2_client.delete_tags(
        Resources=[instance_id],
        Tags=[{"Key": PATCH_INSTALL_APPROVED_TAG_KEY}],
    )
    LOGGER.info("deleted patch install approved tag instance_id=%s", instance_id)


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse an ISO timestamp from DynamoDB into a timezone-aware datetime."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def compute_install_grace_until(
    expected_install_window_at: str | None, approved_for_install_at: datetime
) -> str:
    """Compute the grace deadline using the expected window when available, otherwise approval time."""
    expected_at = parse_timestamp(expected_install_window_at)
    if expected_at is None:
        expected_at = approved_for_install_at
    if expected_at.tzinfo is None:
        expected_at = expected_at.replace(tzinfo=UTC)
    return isoformat(expected_at + timedelta(hours=INSTALL_GRACE_HOURS))
