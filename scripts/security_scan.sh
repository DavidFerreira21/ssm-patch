#!/usr/bin/env bash
set -u

# Run local security checks for this repository.
# - SAST for Python: bandit
# - IaC scan for Terraform: checkov
# - Vulnerability/misconfiguration/secret scan: trivy

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPORTS_DIR="${ROOT_DIR}/security-reports/$(date +%Y%m%d-%H%M%S)"
EXIT_CODE=0

mkdir -p "${REPORTS_DIR}"

log() {
  printf '[security-scan] %s\n' "$1"
}

warn() {
  printf '[security-scan][warn] %s\n' "$1"
}

fail() {
  printf '[security-scan][fail] %s\n' "$1"
  EXIT_CODE=1
}

run_and_capture() {
  local step_name="$1"
  shift

  local logfile="${REPORTS_DIR}/${step_name}.log"
  log "running ${step_name}"

  if "$@" >"${logfile}" 2>&1; then
    log "${step_name} finished successfully"
    return 0
  fi

  fail "${step_name} found issues or failed. See ${logfile}"
  return 1
}

has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

resolve_tool() {
  local tool_name="$1"
  local windows_tool="${ROOT_DIR}/.venv/Scripts/${tool_name}.exe"
  local unix_tool="${ROOT_DIR}/.venv/bin/${tool_name}"

  if [[ -x "${windows_tool}" ]]; then
    printf '%s\n' "${windows_tool}"
    return 0
  fi

  if [[ -x "${unix_tool}" ]]; then
    printf '%s\n' "${unix_tool}"
    return 0
  fi

  if has_cmd "${tool_name}"; then
    command -v "${tool_name}"
    return 0
  fi

  return 1
}

find_python_files() {
  find "${ROOT_DIR}/lambdas" -type f -name '*.py'
}

run_bandit() {
  local bandit_bin
  if ! bandit_bin="$(resolve_tool bandit)"; then
    warn "bandit not installed, skipping SAST bandit"
    return
  fi

  run_and_capture "sast-bandit" \
    "${bandit_bin}" -r "${ROOT_DIR}/lambdas"
}

run_checkov() {
  local checkov_bin
  if ! checkov_bin="$(resolve_tool checkov)"; then
    warn "checkov not installed, skipping Terraform SAST"
    return
  fi

  run_and_capture "iac-checkov" \
    "${checkov_bin}" -d "${ROOT_DIR}"
}

run_trivy_fs() {
  local trivy_bin
  if ! trivy_bin="$(resolve_tool trivy)"; then
    warn "trivy not installed, skipping filesystem vulnerability scan"
    return
  fi

  run_and_capture "vuln-trivy-fs" \
    "${trivy_bin}" fs --scanners vuln,misconfig,secret "${ROOT_DIR}"
}

main() {
  log "reports will be written to ${REPORTS_DIR}"
  log "repository root ${ROOT_DIR}"

  if [[ -z "$(find_python_files)" ]]; then
    warn "no python files found under lambdas"
  fi

  run_bandit
  run_checkov
  run_trivy_fs

  if [[ "${EXIT_CODE}" -eq 0 ]]; then
    log "security scan completed without failing steps"
  else
    fail "security scan completed with failures"
  fi

  return "${EXIT_CODE}"
}

main "$@"
