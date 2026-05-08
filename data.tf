data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

locals {
  enabled = var.reboot_automation_enabled

  prefix_name = "${data.aws_region.current.name}-${data.aws_caller_identity.current.account_id}-dev"

  common_tags = {
    ManagedBy = "Terraform"
    Workload  = "ssm-patch-reboot-automation"
  }
}

data "archive_file" "discovery" {
  count       = local.enabled ? 1 : 0
  type        = "zip"
  source_dir  = "${path.module}/lambdas/discovery"
  output_path = "${path.module}/.terraform/discovery.zip"
}

data "archive_file" "executor" {
  count       = local.enabled ? 1 : 0
  type        = "zip"
  source_dir  = "${path.module}/lambdas/executor"
  output_path = "${path.module}/.terraform/executor.zip"
}
