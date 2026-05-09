###########################################
# Account and Region Data
###########################################

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

###########################################
# Shared Locals
###########################################

locals {
  enabled = var.patch_install_automation_enabled

  prefix_name = "${data.aws_region.current.name}-${data.aws_caller_identity.current.account_id}-dev"
  dynamodb_table_name = coalesce(
    var.dynamodb_resource_name_override,
    "ddb-${local.prefix_name}"
  )
  active_requests_index_name = "gsi1-${local.prefix_name}"
  dynamodb_table_arn         = aws_dynamodb_table.install_requests[0].arn
  dynamodb_stream_arn        = aws_dynamodb_table.install_requests[0].stream_arn

  common_tags = {
    ManagedBy = "Terraform"
    Workload  = "ssm-patch-install-automation"
  }
}

###########################################
# Lambda Package Archives
###########################################

data "archive_file" "discovery" {
  count       = local.enabled ? 1 : 0
  type        = "zip"
  source_dir  = "${path.module}/lambdas/discovery"
  output_path = "${path.module}/lambdas/discovery.zip"
}

data "archive_file" "executor" {
  count       = local.enabled ? 1 : 0
  type        = "zip"
  source_dir  = "${path.module}/lambdas/executor"
  output_path = "${path.module}/lambdas/executor.zip"
}
