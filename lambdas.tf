resource "aws_iam_role" "discovery_lambda" {
  count = local.enabled ? 1 : 0
  name  = "discovery-${local.prefix_name}-role"

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
  name  = "discovery-${local.prefix_name}-policy"
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
          "ssm:DescribeMaintenanceWindows",
          "ssm:ListResourceComplianceSummaries"
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
          "${aws_dynamodb_table.reboot_requests[0].arn}/index/gsi1-${local.prefix_name}"
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
        Resource = "arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:*"
      }
    ]
  })
}

resource "aws_iam_role" "executor_lambda" {
  count = local.enabled ? 1 : 0
  name  = "executor-${local.prefix_name}-role"

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
  name  = "executor-${local.prefix_name}-policy"
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
          "${aws_dynamodb_table.reboot_requests[0].arn}/index/gsi1-${local.prefix_name}"
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
        Resource = "arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:*"
      }
    ]
  })
}

resource "aws_cloudwatch_log_group" "discovery" {
  count             = local.enabled ? 1 : 0
  name              = "/aws/lambda/discovery-${local.prefix_name}"
  retention_in_days = var.retention_days

  tags = local.common_tags
}

resource "aws_cloudwatch_log_group" "executor" {
  count             = local.enabled ? 1 : 0
  name              = "/aws/lambda/executor-${local.prefix_name}"
  retention_in_days = var.retention_days

  tags = local.common_tags
}

resource "aws_lambda_function" "discovery" {
  count            = local.enabled ? 1 : 0
  function_name    = "discovery-${local.prefix_name}"
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
      ACTIVE_REQUESTS_INDEX_NAME  = "gsi1-${local.prefix_name}"
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
  function_name    = "executor-${local.prefix_name}"
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
      ACTIVE_REQUESTS_INDEX_NAME = "gsi1-${local.prefix_name}"
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
  name                = "discovery-${local.prefix_name}-schedule"
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
            region = {
              S = [data.aws_region.current.region]
            }
            status = {
              S = ["APPROVED", "AUTO_APPROVED"]
            }
          }
        }
      })
    }
  }
}
