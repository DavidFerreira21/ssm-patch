###########################################
# Core Automation Outputs
###########################################

output "dynamodb_table_name" {
  value = try(aws_dynamodb_table.install_requests[0].name, null)
}

output "dynamodb_table_arn" {
  value = local.dynamodb_table_arn
}

output "discovery_lambda_name" {
  value = try(aws_lambda_function.discovery[0].function_name, null)
}

output "executor_lambda_name" {
  value = try(aws_lambda_function.executor[0].function_name, null)
}

output "dynamodb_stream_arn" {
  value = local.dynamodb_stream_arn
}

output "active_requests_index_name" {
  value = try(one(aws_dynamodb_table.install_requests[0].global_secondary_index).name, null)
}

###########################################
# SSM Automation Outputs
###########################################

output "automation_document_name" {
  description = "Name of the Automation document used by the install Maintenance Window."
  value       = try(aws_ssm_document.install_patches_and_cleanup[0].name, null)
}

output "automation_role_arn" {
  description = "IAM role ARN assumed by Maintenance Window tasks and Automation executions."
  value       = try(aws_iam_role.ssm_automation[0].arn, null)
}

output "ec2_instance_profile_name" {
  description = "EC2 instance profile name to attach to instances that should register in Systems Manager."
  value       = try(aws_iam_instance_profile.ec2_ssm_managed_instance[0].name, null)
}

output "ec2_role_arn" {
  description = "IAM role ARN attached to the EC2 instance profile for Systems Manager managed instances."
  value       = try(aws_iam_role.ec2_ssm_managed_instance[0].arn, null)
}

output "install_maintenance_window_ids" {
  description = "IDs of the Maintenance Windows that install patches, restart when required by patch installation, and clean the approval tag."
  value = {
    for window_name, window in aws_ssm_maintenance_window.install : window_name => window.id
  }
}

output "scan_maintenance_window_id" {
  description = "ID of the Maintenance Window that scans tagged instances for missing patches."
  value       = try(aws_ssm_maintenance_window.scan[0].id, null)
}
