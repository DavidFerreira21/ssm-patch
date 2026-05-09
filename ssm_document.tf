###########################################
# Automation IAM
###########################################

resource "aws_iam_role" "ssm_automation" {
  count = local.enabled ? 1 : 0
  name  = "ssm-automation-${local.prefix_name}-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "ssm.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy" "ssm_automation" {
  count = local.enabled ? 1 : 0
  name  = "ssm-automation-${local.prefix_name}-policy"
  role  = aws_iam_role.ssm_automation[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "PatchInstallAutomation"
        Effect = "Allow"
        Action = [
          "ssm:DescribeInstanceInformation",
          "ssm:ListCommands",
          "ssm:SendCommand",
          "ssm:StartAutomationExecution",
          "ssm:GetAutomationExecution",
          "ssm:GetCommandInvocation",
          "ssm:ListCommandInvocations"
        ]
        Resource = "*"
      },
      {
        Sid    = "Ec2DeletePatchInstallApprovalTag"
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
        Sid    = "Ec2DescribeInstances"
        Effect = "Allow"
        Action = [
          "ec2:DescribeInstances"
        ]
        Resource = "*"
      },
      {
        Sid    = "PassAutomationRole"
        Effect = "Allow"
        Action = [
          "iam:PassRole"
        ]
        Resource = aws_iam_role.ssm_automation[0].arn
        Condition = {
          StringEquals = {
            "iam:PassedToService" = "ssm.amazonaws.com"
          }
        }
      }
    ]
  })
}

###########################################
# Install Automation Document
###########################################

resource "aws_ssm_document" "install_patches_and_cleanup" {
  count           = local.enabled ? 1 : 0
  name            = "install-patches-and-cleanup-${local.prefix_name}"
  document_type   = "Automation"
  document_format = "JSON"

  content = jsonencode({
    description   = "Automation that installs patches, allows restart when required by patch installation, waits for the instance to be running again, and removes the PatchInstallApproved tag."
    schemaVersion = "0.3"
    assumeRole    = "{{ AutomationAssumeRole }}"
    parameters = {
      AutomationAssumeRole = {
        type        = "String"
        description = "IAM role ARN assumed by the Automation execution."
      }
      InstanceId = {
        type        = "String"
        description = "ID of the EC2 instance that will install patches."
      }
      InstallApprovalTagKey = {
        type        = "String"
        description = "Tag key removed when the install flow finishes successfully."
        default     = var.patch_install_approved_tag_key
      }
      RetryDelaySeconds = {
        type        = "String"
        description = "Seconds to wait before retrying when the patch baseline lock is busy."
        default     = tostring(var.patch_install_retry_delay_minutes * 60)
      }
    }
    mainSteps = [
      {
        name      = "InstallPatchesAttempt1"
        action    = "aws:runCommand"
        nextStep  = "WaitForInstanceRunning"
        onFailure = "step:WaitBeforeRetryAttempt2"
        inputs = {
          DocumentName = "AWS-RunPatchBaseline"
          InstanceIds  = ["{{ InstanceId }}"]
          Parameters = {
            Operation    = ["Install"]
            RebootOption = ["RebootIfNeeded"]
          }
        }
        description = "Runs patch installation and lets the SSM patch document restart the instance when required."
      },
      {
        name     = "WaitBeforeRetryAttempt2"
        action   = "aws:sleep"
        nextStep = "InstallPatchesAttempt2"
        inputs = {
          Duration = "PT{{ RetryDelaySeconds }}S"
        }
        description = "Waits before retrying patch installation when another patch operation is still holding the lock."
      },
      {
        name      = "InstallPatchesAttempt2"
        action    = "aws:runCommand"
        nextStep  = "WaitForInstanceRunning"
        onFailure = "step:WaitBeforeRetryAttempt3"
        inputs = {
          DocumentName = "AWS-RunPatchBaseline"
          InstanceIds  = ["{{ InstanceId }}"]
          Parameters = {
            Operation    = ["Install"]
            RebootOption = ["RebootIfNeeded"]
          }
        }
        description = "Retries patch installation after a short wait when the patch baseline lock was busy."
      },
      {
        name     = "WaitBeforeRetryAttempt3"
        action   = "aws:sleep"
        nextStep = "InstallPatchesAttempt3"
        inputs = {
          Duration = "PT{{ RetryDelaySeconds }}S"
        }
        description = "Waits before the last retry of patch installation."
      },
      {
        name     = "InstallPatchesAttempt3"
        action   = "aws:runCommand"
        nextStep = "WaitForInstanceRunning"
        inputs = {
          DocumentName = "AWS-RunPatchBaseline"
          InstanceIds  = ["{{ InstanceId }}"]
          Parameters = {
            Operation    = ["Install"]
            RebootOption = ["RebootIfNeeded"]
          }
        }
        description = "Last retry of patch installation before the Automation fails."
      },
      {
        name   = "WaitForInstanceRunning"
        action = "aws:waitForAwsResourceProperty"
        inputs = {
          Service          = "ec2"
          Api              = "DescribeInstances"
          InstanceIds      = ["{{ InstanceId }}"]
          PropertySelector = "$.Reservations[0].Instances[0].State.Name"
          DesiredValues    = ["running"]
        }
        timeoutSeconds = 900
        description    = "Waits until the EC2 control plane reports the instance state as running after patch installation."
      },
      {
        name   = "DeleteTagPatchInstallApproved"
        action = "aws:executeAwsApi"
        inputs = {
          Service   = "ec2"
          Api       = "DeleteTags"
          Resources = ["{{ InstanceId }}"]
          Tags = [
            {
              Key = "{{ InstallApprovalTagKey }}"
            }
          ]
        }
        description = "Removes the PatchInstallApproved authorization tag after the install flow finishes successfully."
      }
    ]
  })

  tags = local.common_tags
}
