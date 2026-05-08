# SSM Quick Notes

## Objetivo

Este projeto implementa um workflow de aprovação de reboot pós-patch usando:

- DynamoDB para estado das requests
- Lambda `discovery` para descobrir e atualizar requests
- Lambda `executor` para reagir a requests aprovadas
- DynamoDB Streams para acionar o executor
- EventBridge para rodar o discovery periodicamente

O escopo atual é suportar operação regional com tabela DynamoDB central.

## Decisões de arquitetura

### Modelo regional

- O desenho adotado é `1 stack/Lambda por região`.
- `discovery` opera somente na `AWS_REGION` onde foi implantada.
- `executor` também opera somente na `AWS_REGION` onde foi implantada.
- O DynamoDB é central e compartilhado entre as regiões.

### DynamoDB central

- A tabela DynamoDB é única.
- Cada item armazena:
  - `account_id`
  - `region`
  - `instance_id`
- O `pk` inclui região:

```text
ACCOUNT#<account_id>#REGION#<region>#INSTANCE#<instance_id>
```

- O `sk` continua histórico por timestamp + request id:

```text
REBOOT#<timestamp>#<request_id>
```

### Processamento por região

- O `discovery` filtra requests ativas pela `region` do item no DynamoDB.
- O `executor` ignora records do stream cuja `region` não seja a mesma da Lambda.
- O `event source mapping` do `executor` também filtra `NewImage.region`.

## Fluxo funcional

### Discovery

O `discovery`:

1. Consulta o SSM Compliance.
2. Busca itens `Patch` com status `NON_COMPLIANT`.
3. Filtra apenas `ManagedInstance`.
4. Carrega patch state no SSM.
5. Carrega detalhes e tags no EC2.
6. Cria, atualiza, resolve ou falha requests no DynamoDB.

Regras importantes:

- Se `InstalledPendingRebootCount == 0`:
  - resolve a request ativa
  - remove a tag `RebootRequired` se existir
- Se `PatchManagement != true`:
  - faz `skip`
- Se faltar `PatchRebootWindow`:
  - request vai para `MANUAL`
- Se não existir request ativa:
  - cria `PENDING_APPROVAL`
- Se estiver `POSTPONED`:
  - espera ou muda para `AUTO_APPROVED`
- Se estiver `TAGGED_FOR_REBOOT`:
  - observa `grace_until`
  - se expirar e ainda houver pending reboot, muda para `FAILED_REBOOT`

### Executor

O `executor`:

1. Recebe eventos do DynamoDB Stream.
2. Processa apenas transições para:
  - `APPROVED`
  - `AUTO_APPROVED`
3. Recarrega o item mais recente no DynamoDB.
4. Adiciona a tag:

```text
RebootRequired=true
```

5. Atualiza a request para:

```text
TAGGED_FOR_REBOOT
```

6. Define:
  - `tagged_for_reboot_at`
  - `grace_until`

## Premissas adotadas

- O SSM só monitora instâncias com `PatchManagement=true`.
- Por isso o `discovery` começa pelo SSM Compliance, não por EC2.
- A solução é `single-account` por implantação.
- Se precisar suportar outra conta, a recomendação é outro deploy do módulo.
- Não há necessidade atual de compatibilidade com itens antigos no DynamoDB, porque a tabela ainda não foi implantada.
- Não estamos tratando como prioritário o cenário de corrida rara no `executor` entre `create_tags()` e `update_item()`.

## Terraform

### Nome dos recursos

- Os nomes usam um sufixo comum armazenado em:

```hcl
local.prefix_name
```

- Cada recurso monta seu próprio prefixo, por exemplo:
  - `ddb-${local.prefix_name}`
  - `discovery-${local.prefix_name}`
  - `executor-${local.prefix_name}`

### Módulo por região

- A recomendação é chamar o módulo uma vez por região.
- O DynamoDB central pode ser criado por uma única chamada do módulo.
- As demais chamadas regionais podem consumir os outputs da chamada que criou o DynamoDB.

### Feature toggle para DynamoDB

- Foi discutido um toggle para permitir:
  - criar o DynamoDB em uma chamada
  - reutilizar o DynamoDB existente em outras chamadas regionais

Isso é útil para:

- `create_dynamodb = true` na stack central
- `create_dynamodb = false` nas stacks regionais adicionais

## Segurança

### Já validado

- `bandit` no código Python do `discovery` não encontrou issues.
- O principal volume de achados veio do `checkov` no Terraform.

### Ajustes já feitos

- `point_in_time_recovery` foi adicionado no DynamoDB.
- Retenção de logs foi ajustada para `90` dias.

### Pendências conhecidas

- KMS gerenciado pelo cliente no DynamoDB
- KMS nos CloudWatch Log Groups
- KMS nas variáveis de ambiente das Lambdas
- avaliar DLQ para as Lambdas

### Exceções conscientes por enquanto

- Lambda fora de VPC
- sem code signing
- sem reserved concurrency
- sem X-Ray
- retenção de logs menor que 365 dias

## Segurança local

Existe um script local:

```text
scripts/security_scan.sh
```

Ele tenta rodar:

- `bandit`
- `checkov`
- `trivy`

No Windows, o uso via `bash`/WSL pode gerar ruído. Em PowerShell, os comandos podem ser executados manualmente:

```powershell
bandit -r .\lambdas
checkov -d .
trivy fs --scanners vuln,misconfig,secret .
```

## Observações operacionais

- Se houver instâncias em mais de uma região, a stack operacional precisa existir em cada região.
- Maintenance Windows, EventBridge, Lambdas e recursos SSM são regionais.
- IAM é global da conta, mas atende recursos regionais.
- DynamoDB pode permanecer centralizado.
