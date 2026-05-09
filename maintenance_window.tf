###########################################
# Install Maintenance Window
###########################################

locals {
  install_windows_by_name = {
    for window in var.install_windows : window.window_name => window
  }
}

resource "aws_ssm_maintenance_window" "install" {
  for_each                   = local.enabled ? local.install_windows_by_name : {}
  name                       = "${each.key}-install-${local.prefix_name}"
  description                = "Install window that patches approved instances, restarts when required by patch installation, and cleans the approval tag."
  schedule                   = each.value.schedule
  schedule_timezone          = try(each.value.timezone, var.default_schedule_timezone)
  duration                   = 4
  cutoff                     = 3
  allow_unassociated_targets = false
  tags                       = local.common_tags
}

resource "aws_ssm_maintenance_window_target" "install" {
  for_each      = local.enabled ? local.install_windows_by_name : {}
  window_id     = aws_ssm_maintenance_window.install[each.key].id
  resource_type = "INSTANCE"
  name          = "install-target-${each.key}-${local.prefix_name}"
  description   = "Instances in scope for this install window and explicitly approved for patch installation."

  targets {
    key    = "tag:${var.patch_management_tag_key}"
    values = [var.patch_management_tag_value]
  }

  targets {
    key    = "tag:${var.patch_install_window_tag_key}"
    values = [each.key]
  }

  targets {
    key    = "tag:${var.patch_install_approved_tag_key}"
    values = [var.patch_install_approved_tag_value]
  }
}

resource "aws_ssm_maintenance_window_task" "install" {
  for_each         = local.enabled ? local.install_windows_by_name : {}
  window_id        = aws_ssm_maintenance_window.install[each.key].id
  name             = "install-task-${each.key}-${local.prefix_name}"
  description      = "Installs patches, restarts if needed, and removes the approval tag."
  task_type        = "AUTOMATION"
  task_arn         = aws_ssm_document.install_patches_and_cleanup[0].name
  service_role_arn = aws_iam_role.ssm_automation[0].arn
  priority         = 1
  max_concurrency  = "50%"
  max_errors       = "50%"

  targets {
    key    = "WindowTargetIds"
    values = [aws_ssm_maintenance_window_target.install[each.key].id]
  }

  task_invocation_parameters {
    automation_parameters {
      document_version = "$DEFAULT"

      parameter {
        name   = "AutomationAssumeRole"
        values = [aws_iam_role.ssm_automation[0].arn]
      }

      parameter {
        name   = "InstanceId"
        values = ["{{RESOURCE_ID}}"]
      }

      parameter {
        name   = "InstallApprovalTagKey"
        values = [var.patch_install_approved_tag_key]
      }
    }
  }
}

###########################################
# Scan Maintenance Window
###########################################

resource "aws_ssm_maintenance_window" "scan" {
  count                      = local.enabled ? 1 : 0
  name                       = "scan-${local.prefix_name}"
  description                = "Scan window that checks tagged instances for missing patches."
  schedule                   = var.scan_schedule
  schedule_timezone          = var.default_schedule_timezone
  duration                   = 4
  cutoff                     = 3
  allow_unassociated_targets = false
  tags                       = local.common_tags
}

resource "aws_ssm_maintenance_window_target" "scan" {
  count         = local.enabled ? 1 : 0
  window_id     = aws_ssm_maintenance_window.scan[0].id
  resource_type = "INSTANCE"
  name          = "scan-target-${local.prefix_name}"
  description   = "Instances tagged to participate in patch scan."

  targets {
    key    = "tag:${var.patch_management_tag_key}"
    values = [var.patch_management_tag_value]
  }
}

resource "aws_ssm_maintenance_window_task" "scan" {
  count            = local.enabled ? 1 : 0
  window_id        = aws_ssm_maintenance_window.scan[0].id
  name             = "scan-task-${local.prefix_name}"
  description      = "Runs AWS-RunPatchBaseline with Operation=Scan on tagged instances."
  task_type        = "RUN_COMMAND"
  task_arn         = "AWS-RunPatchBaseline"
  service_role_arn = aws_iam_role.ssm_automation[0].arn
  priority         = 1
  max_concurrency  = "100%"
  max_errors       = "100%"

  targets {
    key    = "WindowTargetIds"
    values = [aws_ssm_maintenance_window_target.scan[0].id]
  }

  task_invocation_parameters {
    run_command_parameters {
      comment = "Patch scan execution."

      parameter {
        name   = "Operation"
        values = ["Scan"]
      }
    }
  }
}
