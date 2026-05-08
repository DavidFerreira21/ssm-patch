# Implementar automação de aprovação de reboot pós-patch

## Contexto

As Maintenance Windows do SSM já existem e já funcionam:

- Patch Scan Window
- Patch Install Window com `RebootOption=NoReboot`
- Reboot Maintenance Window
- Target da Reboot Window usando:
  - `PatchRebootWindow=<window-name>`
  - `RebootRequired=true`

Não criar essas janelas novamente.

O escopo agora é criar somente:

1. DynamoDB
2. DynamoDB Streams
3. Lambda Discovery
4. Lambda Executor
5. IAM roles/policies necessárias
6. EventBridge para executar a Lambda Discovery 2x por dia
7. Código Python das duas Lambdas

---

## Objetivo

Criar um workflow de aprovação para reboot pendente após patch.

O sistema deve:

- Identificar instâncias com reboot pendente
- Registrar solicitação no DynamoDB
- Permitir aprovação alterando o status no DynamoDB
- Adicionar a tag `RebootRequired=true` quando aprovado
- Permitir que a Maintenance Window reinicie a instância
- Resolver ou falhar a solicitação em ciclos posteriores do Discovery

---

## Tags usadas

### Obrigatória para participar do processo

```text
PatchManagement=true
Define a janela de reboot
PatchRebootWindow=<window-name>
Tag dinâmica adicionada após aprovação
RebootRequired=true
DynamoDB

Criar tabela DynamoDB com Streams habilitado.

Nome sugerido
ssm-patch-reboot-requests
Chaves
pk = ACCOUNT#<account_id>#INSTANCE#<instance_id>
sk = REBOOT#<timestamp>#<request_id>

A SK deve conter timestamp + UUID para permitir histórico de múltiplas solicitações por instância.

Streams

Habilitar:

StreamViewType = NEW_AND_OLD_IMAGES
TTL

Habilitar TTL no atributo:

expires_at

TTL padrão: 60 dias.

Campos do item DynamoDB

Exemplo de item:

{
  "pk": "ACCOUNT#123456789012#INSTANCE#i-abc123",
  "sk": "REBOOT#2026-05-06T13:00:00Z#a1b2c3d4",
  "request_id": "a1b2c3d4",
  "account_id": "123456789012",
  "region": "us-east-1",
  "instance_id": "i-abc123",
  "hostname": "srv-api-01",
  "owner": "team-x",
  "environment": "prod",
  "patch_reboot_window": "weekly-sunday-02",
  "installed_pending_reboot_count": 3,
  "status": "PENDING_APPROVAL",
  "postpone_count": 0,
  "postponed_until": null,
  "approved_by": null,
  "approved_at": null,
  "tagged_for_reboot_at": null,
  "grace_until": null,
  "resolution_reason": null,
  "last_error": null,
  "created_at": "2026-05-06T13:00:00Z",
  "updated_at": "2026-05-06T13:00:00Z",
  "expires_at": 1772810400
}
Status suportados
Ativos
PENDING_APPROVAL
POSTPONED
APPROVED
AUTO_APPROVED
TAGGED_FOR_REBOOT
Finais / inativos
RESOLVED
FAILED_REBOOT
MANUAL
CANCELLED
INSTANCE_NOT_FOUND
FAILED_CONFIGURATION
CANCELLED_OUT_OF_SCOPE
Lambda Discovery
Trigger

Criar EventBridge Schedule para executar 2x por dia.

Exemplo:

cron(0 9,17 * * ? *)
Responsabilidade

A Lambda Discovery é o cérebro do workflow.

Ela deve:

Buscar itens NON_COMPLIANT no SSM Compliance
Filtrar compliance do tipo Patch
Consultar describe_instance_patch_states
Verificar InstalledPendingRebootCount
Validar tag PatchManagement=true
Validar se existe PatchRebootWindow
Criar ou atualizar solicitação no DynamoDB
Tratar postergação
Tratar grace period
Resolver solicitação se o reboot pendente sumiu
Detectar falha de reboot se o grace period expirou
Não adicionar tag RebootRequired
Fluxo Discovery
1. Buscar NON_COMPLIANT

Usar SSM Compliance:

ssm.list_resource_compliance_summaries(...)

Filtrar:

ComplianceType = Patch
Status = NON_COMPLIANT

Filtrar ResourceType = ManagedInstance na resposta.

Deduplicar por ResourceId.

2. Consultar patch state

Para as instâncias encontradas, chamar:

ssm.describe_instance_patch_states(...)

Processar em batch de até 50 IDs.

Campo principal:

InstalledPendingRebootCount
3. Se InstalledPendingRebootCount == 0

Se não existe solicitação ativa:

Não fazer nada

Se existe solicitação ativa:

Remover tag RebootRequired, caso exista
Atualizar status para RESOLVED
Atualizar resolution_reason=MANUAL_OR_EXTERNAL_REBOOT
Atualizar updated_at
4. Se InstalledPendingRebootCount > 0

Validar tags EC2:

Se PatchManagement != true, ignorar
Se não existir PatchRebootWindow, criar/atualizar item com:
status=MANUAL
enviar/registrar alerta futuramente
Se existir PatchRebootWindow, seguir workflow de aprovação
5. Se não existir solicitação ativa

Criar item:

status=PENDING_APPROVAL
6. Se existir POSTPONED

Se now < postponed_until:

Ignorar

Se now >= postponed_until:

Atualizar status para AUTO_APPROVED
Registrar que a postergação expirou
O DynamoDB Stream deve acionar a Lambda Executor
7. Se existir TAGGED_FOR_REBOOT

Se now < grace_until:

Ignorar

Se now >= grace_until e ainda existe pending reboot:

Atualizar status para FAILED_REBOOT
Não criar nova solicitação no mesmo ciclo
No próximo ciclo, como FAILED_REBOOT não é ativo, uma nova solicitação será criada naturalmente se ainda houver pending reboot
8. Se existir PENDING_APPROVAL, APPROVED ou AUTO_APPROVED
Não criar duplicidade
Apenas atualizar metadados se necessário
Postergação

A postergação será feita alterando status no DynamoDB futuramente.

Regra:

max_postpones = 1
postpone_days = 6

Quando status virar POSTPONED, o item deve ter:

{
  "postpone_count": 1,
  "postponed_until": "<now + 6 dias>"
}

Quando o prazo expirar:

Discovery muda para AUTO_APPROVED
Executor adiciona a tag
Notificação futura deve informar que o prazo expirou e a máquina será reiniciada na próxima janela
Lambda Executor
Trigger

DynamoDB Streams.

A Lambda Executor deve ser chamada quando houver mudança de status para:

APPROVED
AUTO_APPROVED
Responsabilidade

A Lambda Executor deve:

Validar transição de status
Validar que o item ainda é a solicitação ativa mais recente
Adicionar tag na EC2:
RebootRequired=true
Atualizar item no DynamoDB:
status=TAGGED_FOR_REBOOT
tagged_for_reboot_at=<now>
grace_until=<now + 8 horas>
updated_at=<now>
Importante

A Executor não deve:

Consultar compliance
Resolver solicitação
Criar nova solicitação
Executar reboot
Remover tag
Maintenance Window

Já existe e não deve ser criada.

Ela funciona assim:

Target:

PatchRebootWindow=<window-name>
AND
RebootRequired=true

Tasks:

Priority 1: reboot da máquina
Priority 2: remove tag RebootRequired

A Maintenance Window não deve atualizar DynamoDB.

A tag RebootRequired é uma autorização temporária para uma tentativa de reboot.

Grace Period

Após a Executor adicionar RebootRequired=true, ela deve gravar:

grace_until = now + 8 horas

O Discovery só pode avaliar falha depois do grace period.

Regra:

Se status=TAGGED_FOR_REBOOT
e now < grace_until:
  ignorar

Se status=TAGGED_FOR_REBOOT
e now >= grace_until
e InstalledPendingRebootCount > 0:
  status=FAILED_REBOOT

Se InstalledPendingRebootCount == 0, o Discovery deve resolver como RESOLVED.

Terraform esperado

Criar arquivos ou blocos para:

DynamoDB
aws_dynamodb_table
Stream habilitado
TTL habilitado
Billing mode: PAY_PER_REQUEST
Lambda Discovery
aws_lambda_function
IAM Role
IAM Policy
CloudWatch Log Group
EventBridge Rule
EventBridge Target
Lambda permission para EventBridge
Lambda Executor
aws_lambda_function
IAM Role
IAM Policy
CloudWatch Log Group
Event source mapping para DynamoDB Stream
IAM Discovery Lambda

Permissões necessárias:

ssm:ListComplianceItems
ssm:DescribeInstancePatchStates
ssm:DescribeInstanceInformation
ec2:DescribeInstances
ec2:DescribeTags
ec2:DeleteTags
dynamodb:PutItem
dynamodb:UpdateItem
dynamodb:Query
sts:GetCallerIdentity
logs:CreateLogGroup
logs:CreateLogStream
logs:PutLogEvents
IAM Executor Lambda

Permissões necessárias:

ec2:CreateTags
dynamodb:UpdateItem
dynamodb:Query
dynamodb:GetItem
sts:GetCallerIdentity
logs:CreateLogGroup
logs:CreateLogStream
logs:PutLogEvents
Variáveis Terraform sugeridas
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
  default = "cron(0 9,17 * * ? *)"
}

variable "retention_days" {
  type    = number
  default = 60
}

variable "postpone_days" {
  type    = number
  default = 6
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
Observações importantes
Não criar Maintenance Windows neste escopo.
Não criar Patch Baselines neste escopo.
Não criar Patch Scan/Install Tasks neste escopo.
Criar apenas DynamoDB, Streams, Lambdas, EventBridge e IAM.
O código deve ser idempotente.
Não pode criar solicitações duplicadas se já existe solicitação ativa.
FAILED_REBOOT não é ativo.
RESOLVED não é ativo.
Se reboot pendente continuar após FAILED_REBOOT, o próximo Discovery cria nova solicitação naturalmente.
Se a máquina for reiniciada manualmente e ficar compliant, o Discovery deve marcar solicitação ativa como RESOLVED.
