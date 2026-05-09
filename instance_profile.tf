###########################################
# EC2 Managed Instance IAM
###########################################

resource "aws_iam_role" "ec2_ssm_managed_instance" {
  count = local.enabled ? 1 : 0
  name  = "ec2-ssm-managed-instance-${local.prefix_name}-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "ec2_ssm_managed_instance_core" {
  count      = local.enabled ? 1 : 0
  role       = aws_iam_role.ec2_ssm_managed_instance[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "ec2_ssm_managed_instance" {
  count = local.enabled ? 1 : 0
  name  = "ec2-ssm-managed-instance-${local.prefix_name}-profile"
  role  = aws_iam_role.ec2_ssm_managed_instance[0].name

  tags = local.common_tags
}
