###########################################
# Feature Toggles and Naming
###########################################

variable "patch_install_automation_enabled" {
  type    = bool
  default = true
}

variable "discovery_schedule_expression" {
  type    = string
  default = "cron(0/5 * * * ? *)"
}

variable "retention_days" {
  description = "CloudWatch Logs retention in days for the Lambda log groups."
  type        = number
  default     = 365
}

###########################################
# Workflow Timing
###########################################

variable "postpone_days" {
  description = "Number of days added to postponed_until each time an install request is postponed."
  type        = number
  default     = 6
}

variable "max_postpones" {
  description = "Maximum number of times an install request can be postponed before discovery marks it as invalid."
  type        = number
  default     = 1
}

variable "install_grace_hours" {
  description = "Number of hours to wait after the expected install window before failing a request that remains NON_COMPLIANT."
  type        = number
  default     = 0.5
}

variable "patch_install_retry_delay_minutes" {
  description = "Number of minutes the install Automation waits before retrying when AWS-RunPatchBaseline is busy with another patching operation."
  type        = number
  default     = 2
}

###########################################
# Tag Configuration
###########################################

variable "patch_management_tag_key" {
  description = "Tag key used to select EC2 instances that participate in patch scan and install."
  type        = string
  default     = "PatchManagement"
}

variable "patch_management_tag_value" {
  description = "Tag value used to select EC2 instances that participate in patch scan and install."
  type        = string
  default     = "true"
}

variable "patch_install_window_tag_key" {
  description = "Tag key that identifies which install window a given instance belongs to."
  type        = string
  default     = "PatchInstallWindow"
}

variable "install_windows" {
  description = "Install windows created by this stack. window_name is used both in the Maintenance Window name and as the PatchInstallWindow tag value."
  type = list(object({
    window_name = string
    schedule    = string
    timezone    = optional(string)
  }))
  default = [
    {
      window_name = "install-window-1"
      schedule    = "cron(0/5 * * * ? *)"
    }
  ]

  validation {
    condition     = length(distinct([for window in var.install_windows : window.window_name])) == length(var.install_windows)
    error_message = "install_windows must not contain duplicated window_name values."
  }
}

variable "patch_install_approved_tag_key" {
  description = "Tag key that authorizes an instance to run in the install Maintenance Window."
  type        = string
  default     = "PatchInstallApproved"
}

variable "patch_install_approved_tag_value" {
  description = "Tag value that authorizes an instance to run in the install Maintenance Window."
  type        = string
  default     = "true"
}

###########################################
# Lambda Configuration
###########################################

variable "lambda_runtime" {
  type    = string
  default = "python3.12"
}

variable "lambda_subnet_ids" {
  description = "Subnet IDs used when deploying the Lambdas inside a VPC. Leave empty to keep the Lambdas outside a VPC."
  type        = list(string)
  default     = []
}

variable "lambda_security_group_ids" {
  description = "Security group IDs attached to the Lambdas when lambda_subnet_ids is provided."
  type        = list(string)
  default     = []

  validation {
    condition     = length(var.lambda_subnet_ids) == 0 || length(var.lambda_security_group_ids) > 0
    error_message = "lambda_security_group_ids must be provided when lambda_subnet_ids is not empty."
  }
}

###########################################
# SSM Schedule Configuration
###########################################

variable "aws_region" {
  description = "AWS region where the SSM resources will be created."
  type        = string
  default     = "us-east-1"
}

variable "scan_schedule" {
  description = "Maintenance Window schedule expression for patch scan executions."
  type        = string
  default     = "cron(0/8 * * * ? *)"
}

variable "default_schedule_timezone" {
  description = "Default timezone used by the scan window and by install windows that do not define a specific timezone."
  type        = string
  default     = "America/Sao_Paulo"
}
