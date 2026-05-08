variable "reboot_automation_enabled" {
  type    = bool
  default = true
}

variable "dynamodb_table_name" {
  type    = string
  default = "ssm-patch-reboot-requests"
}

variable "discovery_schedule_expression" {
  type    = string
  default = "cron(0/5 * * * ? *)"
}

variable "retention_days" {
  type    = number
  default = 90
}

variable "postpone_days" {
  type        = number
  default     = 6
  description = "Number of days added to postponed_until each time a reboot request is postponed."
}

variable "max_postpones" {
  type        = number
  default     = 1
  description = "Maximum number of times a reboot request can be postponed before discovery marks it as invalid."
}

variable "grace_hours" {
  type    = number
  default = 8
}

variable "patch_management_tag_key" {
  type    = string
  default = "PatchManagement"
}

variable "patch_management_tag_value" {
  type    = string
  default = "true"
}

variable "patch_reboot_window_tag_key" {
  type    = string
  default = "PatchRebootWindow"
}

variable "reboot_required_tag_key" {
  type    = string
  default = "RebootRequired"
}

variable "reboot_required_tag_value" {
  type    = string
  default = "true"
}

variable "lambda_runtime" {
  type    = string
  default = "python3.12"
}

variable "lambda_subnet_ids" {
  type        = list(string)
  default     = []
  description = "Subnet IDs used when deploying the Lambdas inside a VPC. Leave empty to keep the Lambdas outside a VPC."
}

variable "lambda_security_group_ids" {
  type        = list(string)
  default     = []
  description = "Security group IDs attached to the Lambdas when lambda_subnet_ids is provided."

  validation {
    condition     = length(var.lambda_subnet_ids) == 0 || length(var.lambda_security_group_ids) > 0
    error_message = "lambda_security_group_ids must be provided when lambda_subnet_ids is not empty."
  }
}
