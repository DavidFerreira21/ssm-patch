# SSM Patch Install Approval Automation

## Visão geral

Este projeto cria uma automação de aprovação humana para **instalação de patches** em instâncias EC2 gerenciadas pelo SSM.

O fluxo usa:

- SSM Maintenance Window de `scan`
- SSM Compliance como fonte de verdade para `NON_COMPLIANT`
- Lambda `discovery`
- DynamoDB + DynamoDB Streams
- Lambda `executor`
- SSM Maintenance Window de `install`
- SSM Automation Document para instalar patches, reiniciar quando necessário, tentar novamente quando o Patch Baseline estiver ocupado e limpar a tag de aprovação

O objetivo é aprovar a **instalação** e não mais um reboot isolado.

## Modelo de tags

Tags fixas:

- `PatchManagement=true`
- `PatchInstallWindow=<valor>`

Tag dinâmica:

- `PatchInstallApproved=true`

## Fluxo detalhado

```text
1. SSM Scan Window executa AWS-RunPatchBaseline com Operation=Scan
2. SSM Compliance marca instâncias como NON_COMPLIANT quando há patch faltando
3. EventBridge dispara a Lambda discovery
4. Discovery:
   - busca instâncias NON_COMPLIANT
   - valida PatchManagement=true
   - valida PatchInstallWindow
   - cria ou reconcilia request no DynamoDB
5. Usuário ou integração altera a request:
   - APPROVED
   - POSTPONED
6. DynamoDB Stream dispara a Lambda executor
7. Executor:
   - valida que a request ainda é a mais recente
   - aplica PatchInstallApproved=true
   - muda status para INSTALL_READY ou AUTO_INSTALL_READY
   - congela expected_install_window_at e install_grace_until
8. SSM Install Window encontra a instância aprovada
9. Documento de automação:
   - instala patches
   - se encontrar o lock `patch-baseline-concurrent.lock`, espera e tenta novamente
   - reinicia se necessário
   - espera a instância voltar para running
   - remove PatchInstallApproved
10. Discovery roda novamente:
   - se a instância ficou COMPLIANT, resolve a request
   - se a tag dinâmica sobrou, remove e resolve
   - se a janela esperada ainda não chegou, continua esperando
   - se a janela passou mas o grace ainda está ativo, continua esperando
   - se continuar NON_COMPLIANT após a janela esperada + grace, marca FAILED_REMEDIATION
```

## Diagrama

```text
+------------------+
| Scan Window      |
| AWS-RunPatchBase |
| Operation=Scan   |
+--------+---------+
         |
         v
+------------------+
| SSM Compliance   |
| NON_COMPLIANT    |
+--------+---------+
         |
         v
+------------------+        +----------------------+
| Lambda Discovery |------->| DynamoDB             |
| create/reconcile |        | request state/history|
+--------+---------+        +----------+-----------+
         |                              |
         |                              v
         |                     +----------------------+
         |                     | DynamoDB Streams     |
         |                     +----------+-----------+
         |                                |
         |                                v
         |                     +----------------------+
         |                     | Lambda Executor      |
         |                     | apply approval tag   |
         |                     +----------+-----------+
         |                                |
         v                                v
+------------------+             +----------------------+
| User / Workflow  |             | PatchInstallApproved |
| approve/postpone |             | = true               |
+------------------+             +----------+-----------+
                                            |
                                            v
                                 +----------------------+
                                 | Install Window       |
                                 | Automation Document  |
                                 +----------+-----------+
                                            |
                                            v
                                 +----------------------+
                                 | Install patches      |
                                 | RebootIfNeeded       |
                                 | Remove approval tag  |
                                 +----------+-----------+
                                            |
                                            v
                                 +----------------------+
                                 | Lambda Discovery     |
                                 | resolve or fail      |
                                 +----------------------+
```

## Status do workflow

Ativos:

- `PENDING_APPROVAL`
- `POSTPONED`
- `APPROVED`
- `AUTO_APPROVED`
- `INSTALL_READY`
- `AUTO_INSTALL_READY`

Finais:

- `RESOLVED`
- `MANUAL`
- `INSTANCE_NOT_FOUND`
- `FAILED_CONFIGURATION`
- `FAILED_REMEDIATION`

## Estrutura Terraform

Arquivos principais:

- [data.tf](./data.tf)
- [dynamodb.tf](./dynamodb.tf)
- [lambdas.tf](./lambdas.tf)
- [maintenance_window.tf](./maintenance_window.tf)
- [ssm_document.tf](./ssm_document.tf)
- [variables.tf](./variables.tf)
- [outputs.tf](./outputs.tf)

## Recursos criados

- DynamoDB para requests
- Lambda `discovery`
- Lambda `executor`
- EventBridge schedule para `discovery`
- DynamoDB Stream mapping para `executor`
- SSM Maintenance Window de `scan`
- SSM Maintenance Window de `install`
- SSM Automation Document de instalação
- IAM role para Automation

## DynamoDB

Chaves:

```text
pk = ACCOUNT#<account_id>#REGION#<region>#INSTANCE#<instance_id>
sk = INSTALL#<timestamp>#<request_id>
```

O item guarda histórico por instância e região, além de metadata como:

- `hostname`
- `owner`
- `environment`
- `patch_install_window`
- `patch_install_window_description`
- `next_install_window_at`
- `expected_install_window_at`
- `install_grace_until`
- `status`
- `postpone_count`
- `postponed_until`

## Deploy

Fluxo típico:

```bash
terraform init
terraform plan
terraform apply
```

Observação:

- o módulo foi pensado para ser chamado `1x por região`
- cada stack cria seu próprio DynamoDB regional

## Install windows

As janelas de instalação são configuradas pela variável `install_windows`, no formato:

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

Regras:

- `window_name`
  - vira o nome lógico da janela
  - também é o valor esperado na tag `PatchInstallWindow`
- `schedule`
  - define o cron da Maintenance Window de instalação
- `timezone`
  - é opcional
  - se omitido, a janela usa `default_schedule_timezone`

## Scans de segurança

Comandos locais úteis:

```powershell
bandit -r .\lambdas
checkov -d .
trivy fs --scanners vuln,misconfig,secret .
```

## GitHub Actions

O repositório possui um workflow em [`.github/workflows/security.yml`](./.github/workflows/security.yml) com três frentes:

- `lint`
  - `terraform fmt -check`
  - `terraform init -backend=false`
  - `terraform validate`
  - `py_compile` dos arquivos Python
- `sast`
  - `bandit`
  - `checkov`
  - `trivy fs`
- `dast`
  - `OWASP ZAP` baseline scan
  - executa somente quando existir uma URL alvo

### Como usar o DAST

O job de DAST é opcional porque este repositório não expõe uma aplicação HTTP local no próprio código.

Você pode informar a URL de duas formas:

- variável do repositório `DAST_TARGET_URL`
- `workflow_dispatch` com o input `dast_target_url`

## Notas

- O `discovery` é o reconciliador do processo.
- O `executor` só marca a instância como aprovada para instalação.
- A Maintenance Window de `install` executa a mudança real.
- O `discovery` fecha o ciclo quando a instância volta a `COMPLIANT`.

## Roadmap

### 1. Testes automatizados

Próximo foco:

- testes unitários para o `discovery`
- testes unitários para o `executor`
- cobertura de:
  - `PENDING_APPROVAL`
  - `POSTPONED`
  - `INSTALL_READY`
  - `AUTO_INSTALL_READY`
  - `RESOLVED`
  - `FAILED_REMEDIATION`

Objetivo:

- proteger a regra de negócio contra regressões
- dar segurança para evoluir status, tags e reconciliação

### 2. Testes de cenário

Próximo foco:

- validar cenários fora do caminho feliz
- confirmar comportamento operacional fim a fim

Cenários prioritários:

- tag `PatchManagement` incorreta
- ausência de `PatchInstallWindow`
- `POSTPONED` inválido
- install falha e a instância continua `NON_COMPLIANT`
- item antigo com `status` e GSI inconsistentes
- conflito entre `scan` e `install`
- transição correta para `FAILED_REMEDIATION`

### 3. Alertas e observabilidade

Próximo foco:

- criar alertas operacionais para falhas importantes
- facilitar troubleshooting sem depender apenas de leitura manual de log

Alertas prioritários:

- `FAILED_REMEDIATION`
- `FAILED_CONFIGURATION`
- erro na Lambda `discovery`
- erro na Lambda `executor`
- falha em SSM Automation / Maintenance Window
