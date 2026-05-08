output "dynamodb_table_name" {
  value = try(aws_dynamodb_table.reboot_requests[0].name, null)
}

output "discovery_lambda_name" {
  value = try(aws_lambda_function.discovery[0].function_name, null)
}

output "executor_lambda_name" {
  value = try(aws_lambda_function.executor[0].function_name, null)
}

output "dynamodb_stream_arn" {
  value = try(aws_dynamodb_table.reboot_requests[0].stream_arn, null)
}

output "active_requests_index_name" {
  value = local.enabled ? "gsi1-${local.prefix_name}" : null
}
