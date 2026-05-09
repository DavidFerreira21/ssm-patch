###########################################
# Discovery Lambda IAM
###########################################

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
    Statement = concat(
      [
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
          Sid    = "EC2DeletePatchInstallApprovalTag"
          Effect = "Allow"
          Action = [
            "ec2:DeleteTags"
          ]
          Resource = "arn:aws:ec2:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:instance/*"
          Condition = {
            "ForAllValues:StringEquals" = {
              "aws:TagKeys" = [var.patch_install_approved_tag_key]
            }
          }
        },
        {
          Sid    = "EC2DescribeInstancesAndTags"
          Effect = "Allow"
          Action = [
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
            local.dynamodb_table_arn,
            "${local.dynamodb_table_arn}/index/${local.active_requests_index_name}"
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
          Sid    = "CloudWatchLogsCreateGroup"
          Effect = "Allow"
          Action = [
            "logs:CreateLogGroup"
          ]
          Resource = "*"
        },
        {
          Sid    = "CloudWatchLogsWriteDiscovery"
          Effect = "Allow"
          Action = [
            "logs:CreateLogStream",
            "logs:PutLogEvents"
          ]
          Resource = [
            "${aws_cloudwatch_log_group.discovery[0].arn}:*"
          ]
        }
      ],
      length(var.lambda_subnet_ids) > 0 ? [
        {
          Sid    = "VpcNetworking"
          Effect = "Allow"
          Action = [
            "ec2:CreateNetworkInterface",
            "ec2:DescribeNetworkInterfaces",
            "ec2:DeleteNetworkInterface",
            "ec2:AssignPrivateIpAddresses",
            "ec2:UnassignPrivateIpAddresses"
          ]
          Resource = "*"
        }
      ] : []
    )
  })
}

###########################################
# Executor Lambda IAM
###########################################

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
    Statement = concat(
      [
        {
          Sid    = "EC2TagInstances"
          Effect = "Allow"
          Action = [
            "ec2:CreateTags"
          ]
          Resource = "arn:aws:ec2:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:instance/*"
          Condition = {
            StringEquals = {
              "aws:RequestTag/${var.patch_install_approved_tag_key}" = var.patch_install_approved_tag_value
            }
            "ForAllValues:StringEquals" = {
              "aws:TagKeys" = [var.patch_install_approved_tag_key]
            }
          }
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
            local.dynamodb_table_arn,
            "${local.dynamodb_table_arn}/index/${local.active_requests_index_name}"
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
          Resource = local.dynamodb_stream_arn
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
          Sid    = "CloudWatchLogsCreateGroup"
          Effect = "Allow"
          Action = [
            "logs:CreateLogGroup"
          ]
          Resource = "*"
        },
        {
          Sid    = "CloudWatchLogsWriteExecutor"
          Effect = "Allow"
          Action = [
            "logs:CreateLogStream",
            "logs:PutLogEvents"
          ]
          Resource = [
            "${aws_cloudwatch_log_group.executor[0].arn}:*"
          ]
        }
      ],
      length(var.lambda_subnet_ids) > 0 ? [
        {
          Sid    = "VpcNetworking"
          Effect = "Allow"
          Action = [
            "ec2:CreateNetworkInterface",
            "ec2:DescribeNetworkInterfaces",
            "ec2:DeleteNetworkInterface",
            "ec2:AssignPrivateIpAddresses",
            "ec2:UnassignPrivateIpAddresses"
          ]
          Resource = "*"
        }
      ] : []
    )
  })
}

###########################################
# Lambda Log Groups
###########################################

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

##########################################
# Lambda Functions
##########################################

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
      DDB_TABLE_NAME                   = local.dynamodb_table_name
      ACTIVE_REQUESTS_INDEX_NAME       = local.active_requests_index_name
      PATCH_MANAGEMENT_TAG_KEY         = var.patch_management_tag_key
      PATCH_MANAGEMENT_TAG_VALUE       = var.patch_management_tag_value
      PATCH_INSTALL_WINDOW_TAG_KEY     = var.patch_install_window_tag_key
      PATCH_INSTALL_APPROVED_TAG_KEY   = var.patch_install_approved_tag_key
      PATCH_INSTALL_APPROVED_TAG_VALUE = var.patch_install_approved_tag_value
      INSTALL_GRACE_HOURS              = tostring(var.install_grace_hours)
      MAX_POSTPONES                    = tostring(var.max_postpones)
      POSTPONE_DAYS                    = tostring(var.postpone_days)
      RETENTION_DAYS                   = tostring(var.retention_days)
    }
  }

  dynamic "vpc_config" {
    for_each = length(var.lambda_subnet_ids) > 0 ? [1] : []

    content {
      subnet_ids         = var.lambda_subnet_ids
      security_group_ids = var.lambda_security_group_ids
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
      DDB_TABLE_NAME                   = local.dynamodb_table_name
      ACTIVE_REQUESTS_INDEX_NAME       = local.active_requests_index_name
      PATCH_INSTALL_APPROVED_TAG_KEY   = var.patch_install_approved_tag_key
      PATCH_INSTALL_APPROVED_TAG_VALUE = var.patch_install_approved_tag_value
      INSTALL_GRACE_HOURS              = tostring(var.install_grace_hours)
    }
  }

  dynamic "vpc_config" {
    for_each = length(var.lambda_subnet_ids) > 0 ? [1] : []

    content {
      subnet_ids         = var.lambda_subnet_ids
      security_group_ids = var.lambda_security_group_ids
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.executor
  ]

  tags = local.common_tags
}

###########################################
# Discovery Schedule
###########################################

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

###########################################
# Executor Stream Mapping
###########################################

resource "aws_lambda_event_source_mapping" "executor_stream" {
  count             = local.enabled ? 1 : 0
  event_source_arn  = local.dynamodb_stream_arn
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
