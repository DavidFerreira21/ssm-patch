###########################################
# Discovery Notifications
###########################################

resource "aws_secretsmanager_secret" "discovery_teams_webhook" {
  count = local.enabled ? 1 : 0
  name  = "discovery-teams-webhook-${local.prefix_name}"

  description = "Microsoft Teams incoming webhook used by the discovery lambda."

  tags = local.common_tags
}
