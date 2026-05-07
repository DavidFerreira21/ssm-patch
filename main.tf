data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

data "aws_partition" "current" {}

locals {
  enabled = var.reboot_automation_enabled

  discovery_lambda_name = "${var.dynamodb_table_name}-discovery"
  executor_lambda_name  = "${var.dynamodb_table_name}-executor"
  active_requests_index = "gsi1-active-requests"

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

resource "aws_dynamodb_table" "reboot_requests" {
  count        = local.enabled ? 1 : 0
  name         = var.dynamodb_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  attribute {
    name = "gsi1pk"
    type = "S"
  }

  attribute {
    name = "gsi1sk"
    type = "S"
  }

  global_secondary_index {
    name            = local.active_requests_index
    hash_key        = "gsi1pk"
    range_key       = "gsi1sk"
    projection_type = "ALL"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  stream_enabled   = true
  stream_view_type = "NEW_AND_OLD_IMAGES"

  tags = local.common_tags
}

resource "aws_iam_role" "discovery_lambda" {
  count = local.enabled ? 1 : 0
  name  = "${local.discovery_lambda_name}-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy" "discovery_lambda" {
  count = local.enabled ? 1 : 0
  name  = "${local.discovery_lambda_name}-policy"
  role  = aws_iam_role.discovery_lambda[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "SSMReadPatchCompliance"
        Effect = "Allow"
        Action = [
          "ssm:DescribeInstanceInformation",
          "ssm:DescribeInstancePatchStates",
          "ssm:ListComplianceItems"
        ]
        Resource = "*"
      },
      {
        Sid    = "EC2ReadAndCleanupTags"
        Effect = "Allow"
        Action = [
          "ec2:DeleteTags",
          "ec2:DescribeInstances",
          "ec2:DescribeTags"
        ]
        Resource = "*"
      },
      {
        Sid    = "DynamoDbWorkflowState"
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:Query",
          "dynamodb:UpdateItem"
        ]
        Resource = [
          aws_dynamodb_table.reboot_requests[0].arn,
          "${aws_dynamodb_table.reboot_requests[0].arn}/index/${local.active_requests_index}"
        ]
      },
      {
        Sid    = "StsIdentity"
        Effect = "Allow"
        Action = [
          "sts:GetCallerIdentity"
        ]
        Resource = "*"
      },
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:${data.aws_partition.current.partition}:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:*"
      }
    ]
  })
}

resource "aws_iam_role" "executor_lambda" {
  count = local.enabled ? 1 : 0
  name  = "${local.executor_lambda_name}-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy" "executor_lambda" {
  count = local.enabled ? 1 : 0
  name  = "${local.executor_lambda_name}-policy"
  role  = aws_iam_role.executor_lambda[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "EC2TagInstances"
        Effect = "Allow"
        Action = [
          "ec2:CreateTags"
        ]
        Resource = "*"
      },
      {
        Sid    = "DynamoDbWorkflowState"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:Query",
          "dynamodb:UpdateItem"
        ]
        Resource = [
          aws_dynamodb_table.reboot_requests[0].arn,
          "${aws_dynamodb_table.reboot_requests[0].arn}/index/${local.active_requests_index}"
        ]
      },
      {
        Sid    = "DynamoDbStreamRead"
        Effect = "Allow"
        Action = [
          "dynamodb:DescribeStream",
          "dynamodb:GetRecords",
          "dynamodb:GetShardIterator",
          "dynamodb:ListStreams"
        ]
        Resource = aws_dynamodb_table.reboot_requests[0].stream_arn
      },
      {
        Sid    = "StsIdentity"
        Effect = "Allow"
        Action = [
          "sts:GetCallerIdentity"
        ]
        Resource = "*"
      },
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:${data.aws_partition.current.partition}:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:*"
      }
    ]
  })
}

resource "aws_cloudwatch_log_group" "discovery" {
  count             = local.enabled ? 1 : 0
  name              = "/aws/lambda/${local.discovery_lambda_name}"
  retention_in_days = var.retention_days

  tags = local.common_tags
}

resource "aws_cloudwatch_log_group" "executor" {
  count             = local.enabled ? 1 : 0
  name              = "/aws/lambda/${local.executor_lambda_name}"
  retention_in_days = var.retention_days

  tags = local.common_tags
}

resource "aws_lambda_function" "discovery" {
  count            = local.enabled ? 1 : 0
  function_name    = local.discovery_lambda_name
  role             = aws_iam_role.discovery_lambda[0].arn
  handler          = "app.lambda_handler"
  runtime          = var.lambda_runtime
  filename         = data.archive_file.discovery[0].output_path
  source_code_hash = data.archive_file.discovery[0].output_base64sha256
  timeout          = 300
  memory_size      = 512

  environment {
    variables = {
      DDB_TABLE_NAME              = aws_dynamodb_table.reboot_requests[0].name
      ACTIVE_REQUESTS_INDEX_NAME  = local.active_requests_index
      PATCH_MANAGEMENT_TAG_KEY    = var.patch_management_tag_key
      PATCH_MANAGEMENT_TAG_VALUE  = var.patch_management_tag_value
      PATCH_REBOOT_WINDOW_TAG_KEY = var.patch_reboot_window_tag_key
      REBOOT_REQUIRED_TAG_KEY     = var.reboot_required_tag_key
      MAX_POSTPONES               = tostring(var.max_postpones)
      POSTPONE_DAYS               = tostring(var.postpone_days)
      RETENTION_DAYS              = tostring(var.retention_days)
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.discovery
  ]

  tags = local.common_tags
}

resource "aws_lambda_function" "executor" {
  count            = local.enabled ? 1 : 0
  function_name    = local.executor_lambda_name
  role             = aws_iam_role.executor_lambda[0].arn
  handler          = "app.lambda_handler"
  runtime          = var.lambda_runtime
  filename         = data.archive_file.executor[0].output_path
  source_code_hash = data.archive_file.executor[0].output_base64sha256
  timeout          = 120
  memory_size      = 256

  environment {
    variables = {
      DDB_TABLE_NAME             = aws_dynamodb_table.reboot_requests[0].name
      GRACE_HOURS                = tostring(var.grace_hours)
      ACTIVE_REQUESTS_INDEX_NAME = local.active_requests_index
      REBOOT_REQUIRED_TAG_KEY    = var.reboot_required_tag_key
      REBOOT_REQUIRED_TAG_VALUE  = var.reboot_required_tag_value
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.executor
  ]

  tags = local.common_tags
}

resource "aws_cloudwatch_event_rule" "discovery" {
  count               = local.enabled ? 1 : 0
  name                = "${local.discovery_lambda_name}-schedule"
  schedule_expression = var.discovery_schedule_expression

  tags = local.common_tags
}

resource "aws_cloudwatch_event_target" "discovery" {
  count     = local.enabled ? 1 : 0
  rule      = aws_cloudwatch_event_rule.discovery[0].name
  target_id = "discovery-lambda"
  arn       = aws_lambda_function.discovery[0].arn
}

resource "aws_lambda_permission" "eventbridge_invoke_discovery" {
  count         = local.enabled ? 1 : 0
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.discovery[0].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.discovery[0].arn
}

resource "aws_lambda_event_source_mapping" "executor_stream" {
  count             = local.enabled ? 1 : 0
  event_source_arn  = aws_dynamodb_table.reboot_requests[0].stream_arn
  function_name     = aws_lambda_function.executor[0].arn
  starting_position = "LATEST"
  batch_size        = 10
  enabled           = true

  filter_criteria {
    filter {
      pattern = jsonencode({
        eventName = ["INSERT", "MODIFY"]
        dynamodb = {
          NewImage = {
            status = {
              S = ["APPROVED", "AUTO_APPROVED"]
            }
          }
        }
      })
    }
  }
}
