# SSM Quick Notes

## Objetivo

Este projeto implementa um workflow de aprovação humana para **instalação de patches**, não mais para reboot isolado.

A automação usa:

- DynamoDB para estado das requests
- Lambda `discovery` para descobrir e reconciliar requests
- Lambda `executor` para reagir a requests aprovadas
- DynamoDB Streams para acionar o `executor`
- EventBridge para rodar o `discovery`
- SSM Maintenance Windows para `scan` e `install`
- SSM Automation Document para instalar patches, reiniciar quando necessário, tentar novamente quando o Patch Baseline estiver ocupado e limpar a tag dinâmica

## Modelo funcional atual

### Tags

Tags fixas:

- `PatchManagement=true`
- `PatchInstallWindow=<valor>`

Tag dinâmica:

- `PatchInstallApproved=true`

### Significado

- `PatchManagement=true`
  coloca a instância no programa de patch
- `PatchInstallWindow=<valor>`
  indica em qual janela lógica de instalação a instância pertence
- `PatchInstallApproved=true`
  é a autorização operacional temporária para a janela de instalação executar

## Desenho do fluxo

```text
SSM Scan Window
    |
    v
SSM Compliance = NON_COMPLIANT
    |
    v
Lambda Discovery
    |
    +--> se PatchManagement != true -> ignora
    |
    +--> se faltar PatchInstallWindow -> MANUAL
    |
    +--> se não existir request ativa -> cria PENDING_APPROVAL
    |
    +--> se POSTPONED ainda vigente -> mantém
    |
    +--> se POSTPONED expirou -> AUTO_APPROVED
    |
    +--> se APPROVED/AUTO_APPROVED/READY -> só reconcilia metadata
    |
    v
Usuário / sistema altera status
    |
    +--> APPROVED
    |
    +--> POSTPONED
    |
    v
DynamoDB Stream
    |
    v
Lambda Executor
    |
    +--> aplica PatchInstallApproved=true
    |
    +--> APPROVED -> INSTALL_READY
    |
    +--> AUTO_APPROVED -> AUTO_INSTALL_READY
    |
    +--> congela expected_install_window_at e install_grace_until
    |
    v
SSM Install Window
    |
    +--> alvo:
    |    PatchManagement=true
    |    PatchInstallWindow=<valor>
    |    PatchInstallApproved=true
    |
    +--> instala patches
    |
    +--> se encontrar patch-baseline-concurrent.lock:
    |    espera e tenta novamente
    |
    +--> reinicia se necessário
    |
    +--> remove PatchInstallApproved
    |
    v
Lambda Discovery em ciclos seguintes
    |
    +--> se instância ficou COMPLIANT -> RESOLVED
    |
    +--> se instância ficou COMPLIANT e a tag dinâmica sobrou -> remove tag e RESOLVED
    |
    +--> se a janela esperada ainda não chegou -> espera
    |
    +--> se a janela passou mas o grace ainda está ativo -> espera
    |
    +--> se continuar NON_COMPLIANT após a janela esperada + grace -> FAILED_REMEDIATION
```

## Status

Ativos:

- `PENDING_APPROVAL`
- `POSTPONED`
- `APPROVED`
- `AUTO_APPROVED`
- `INSTALL_READY`
- `AUTO_INSTALL_READY`

Finais / fora do fluxo ativo:

- `RESOLVED`
- `MANUAL`
- `INSTANCE_NOT_FOUND`
- `FAILED_CONFIGURATION`
- `FAILED_REMEDIATION`

## Decisões de arquitetura

### Modelo regional

- O desenho adotado é `1 stack por região`.
- `discovery` opera somente na `AWS_REGION` onde foi implantada.
- `executor` também opera somente na `AWS_REGION` onde foi implantada.
- O DynamoDB também é regional e pertence à mesma stack.

### DynamoDB regional

- Cada stack cria sua própria tabela DynamoDB.
- Cada item armazena:
  - `account_id`
  - `region`
  - `instance_id`
- O `pk` inclui região:

```text
ACCOUNT#<account_id>#REGION#<region>#INSTANCE#<instance_id>
```

- O `sk` guarda histórico por timestamp + request id:

```text
INSTALL#<timestamp>#<request_id>
```

### Processamento por região

- O `discovery` filtra requests ativas pela `region` do item no DynamoDB.
- O `executor` ignora records do stream cuja `region` não seja a mesma da Lambda.
- O `event source mapping` do `executor` também filtra `NewImage.region`.
- O stream do DynamoDB fica na mesma região da Lambda `executor`.

## Discovery

O `discovery`:

1. Consulta o SSM Compliance.
2. Busca instâncias `Patch` com status `NON_COMPLIANT`.
3. Carrega detalhes e tags no EC2.
4. Enriquece metadata da janela:
   - `patch_install_window`
   - `patch_install_window_description`
   - `next_install_window_at`
5. Cria, atualiza, resolve ou coloca requests em `MANUAL`.

Regras principais:

- Se `PatchManagement != true`:
  - ignora
- Se faltar `PatchInstallWindow`:
  - vai para `MANUAL`
- Se a instância estiver `COMPLIANT`:
  - resolve a request ativa
  - remove `PatchInstallApproved` se a tag ainda estiver presente
- Se não existir request ativa:
  - cria `PENDING_APPROVAL`
- Se estiver `POSTPONED`:
  - espera ou muda para `AUTO_APPROVED`
- Se estiver ativa em estado refreshable:
  - atualiza metadata sem duplicar request

## Executor

O `executor`:

1. Recebe eventos do DynamoDB Stream.
2. Processa apenas transições para:
   - `APPROVED`
   - `AUTO_APPROVED`
3. Recarrega o item mais recente no DynamoDB.
4. Adiciona a tag:

```text
PatchInstallApproved=true
```

5. Atualiza a request para:

```text
INSTALL_READY
AUTO_INSTALL_READY
```

6. Congela também:

```text
approved_for_install_at
expected_install_window_at
install_grace_until
```

O `executor` não:

- consulta compliance
- resolve request
- cria request
- executa instalação
- remove tag

## Maintenance Windows

### Scan

- Executa `AWS-RunPatchBaseline`
- `Operation=Scan`
- alvo:
  - `PatchManagement=true`

### Install

- Usa documento de automação customizado
- alvo:
  - `PatchManagement=true`
  - `PatchInstallWindow=<valor>`
  - `PatchInstallApproved=true`
- as janelas de instalação são definidas por `install_windows`
- cada janela possui:
  - `window_name`
  - `schedule`
  - `timezone` opcional
- se a janela não informar timezone:
  - usa `default_schedule_timezone`

O documento:

1. roda `AWS-RunPatchBaseline`
2. usa `Operation=Install`
3. usa `RebootIfNeeded`
4. espera a instância voltar para `running`
5. remove `PatchInstallApproved`

### Falha de remediação

Quando a request entra em:

- `INSTALL_READY`
- `AUTO_INSTALL_READY`

o sistema congela:

- `expected_install_window_at`
- `install_grace_until`

No `discovery`:

- se a instância estiver `COMPLIANT`:
  - `RESOLVED`
- se `now < expected_install_window_at`:
  - espera
- se `expected_install_window_at <= now < install_grace_until`:
  - espera
- se `now >= install_grace_until` e a instância ainda estiver `NON_COMPLIANT`:
  - `FAILED_REMEDIATION`

## Postergação

Parâmetros:

- `max_postpones`
- `postpone_days`

Quando uma request vai para `POSTPONED`, ela precisa ter:

- `postpone_count`
- `postponed_until`

Quando o prazo expira:

- o `discovery` muda para `AUTO_APPROVED`
- o stream aciona o `executor`

## Terraform

### Nome dos recursos

- Os nomes usam um sufixo comum armazenado em:

```hcl
local.prefix_name
```

- Cada recurso monta seu próprio nome, por exemplo:
  - `ddb-${local.prefix_name}`
  - `discovery-${local.prefix_name}`
  - `executor-${local.prefix_name}`

### Módulo por região

- A recomendação é chamar o módulo uma vez por região.
- Cada chamada cria:
  - DynamoDB
  - stream
  - Lambdas
  - EventBridge
  - Maintenance Windows
  - documento de automação

### Exemplo de install windows

```hcl
install_windows = [
  {
    window_name = "poc-window-1"
    schedule    = "cron(0 22 ? * SUN *)"
    timezone    = "America/Sao_Paulo"
  },
  {
    window_name = "poc-window-2"
    schedule    = "cron(0 23 ? * MON,WED *)"
  }
]
```

## Segurança

### Já validado

- `bandit` no Python não encontrou issues relevantes no fluxo principal.
- O principal volume de achados veio do `checkov` no Terraform.

### Ajustes já feitos

- `point_in_time_recovery` no DynamoDB
- retenção de logs em `365` dias
- tratamento de falha por instância no `discovery`
- tratamento de falha por record no `executor`
- paginação de maintenance windows

### Pendências conhecidas

- KMS gerenciado pelo cliente no DynamoDB
- KMS nos CloudWatch Log Groups
- avaliar KMS para variáveis de ambiente das Lambdas
- avaliar DLQ para as Lambdas

### Exceções conscientes por enquanto

- Lambda fora de VPC por padrão
- sem code signing
- sem reserved concurrency
- sem X-Ray

## Roadmap

### 1. Testes automatizados

Objetivo:

- cobrir a regra de negócio principal do `discovery` e do `executor`
- reduzir risco de regressão quando houver ajuste de status, tags ou reconciliação

Prioridades:

- testes unitários para criação de `PENDING_APPROVAL`
- testes unitários para `POSTPONED`, incluindo prazo válido e inválido
- testes unitários para transição para `INSTALL_READY` e `AUTO_INSTALL_READY`
- testes unitários para `RESOLVED` quando a instância fica `COMPLIANT`
- testes unitários para `FAILED_REMEDIATION` após `expected_install_window_at + install_grace_until`
- testes para limpeza da tag `PatchInstallApproved`

### 2. Testes de cenário

Objetivo:

- validar fluxos menos felizes que não aparecem no caminho feliz
- confirmar reconciliação correta entre SSM, DynamoDB, tags e janelas

Cenários prioritários:

- instância com `PatchManagement` errado
- instância sem `PatchInstallWindow`
- `POSTPONED` escrito manualmente sem todos os atributos obrigatórios
- install executado, tag removida e instância ainda `NON_COMPLIANT`
- request antiga com `status` e `gsi1pk` inconsistentes
- colisão entre `scan` e `install` com retry do documento
- request que deve virar `FAILED_REMEDIATION`

### 3. Alertas e observabilidade

Objetivo:

- detectar falhas operacionais cedo
- reduzir troubleshooting manual em CloudWatch Logs

Alertas prioritários:

- requests em `FAILED_REMEDIATION`
- requests em `FAILED_CONFIGURATION`
- erros na Lambda `discovery`
- erros na Lambda `executor`
- falhas no SSM Automation / Maintenance Window

Melhorias desejadas:

- métricas simples por status final
- logs mais explícitos para casos de skip por tag
- painel básico para requests ativas e falhas recentes
