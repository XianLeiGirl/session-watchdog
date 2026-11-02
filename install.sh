#!/usr/bin/env bash

set -euo pipefail

REPOSITORY="XianLeiGirl/session-watchdog"
ARCHIVE_URL="${SESSION_WATCHDOG_ARCHIVE_URL:-https://github.com/${REPOSITORY}/archive/refs/heads/main.tar.gz}"
INSTALL_DIR="${SESSION_WATCHDOG_INSTALL_DIR:-${HOME:?HOME is required}/session-watchdog}"
BIN_DIR="${SESSION_WATCHDOG_BIN_DIR:-${HOME}/.local/bin}"
LAUNCHER="${BIN_DIR}/session-watchdog-plugin"
TEMP_DIR=""

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

cleanup() {
  if [[ -n "${TEMP_DIR}" && -d "${TEMP_DIR}" ]]; then
    find "${TEMP_DIR}" -depth -delete 2>/dev/null || true
  fi
}

trap cleanup EXIT

source_dir=""
script_file="${BASH_SOURCE[0]:-}"
if [[ -n "${script_file}" && -f "${script_file}" ]]; then
  candidate_dir="$(cd -- "$(dirname -- "${script_file}")" && pwd -P)"
  if [[ -f "${candidate_dir}/scripts/pluginctl.py" ]]; then
    source_dir="${candidate_dir}"
  fi
fi

if [[ -z "${source_dir}" && -f "${INSTALL_DIR}/scripts/pluginctl.py" ]]; then
  source_dir="$(cd -- "${INSTALL_DIR}" && pwd -P)"
  printf 'Using existing installation: %s\n' "${source_dir}"
fi

if [[ -z "${source_dir}" ]]; then
  case "${INSTALL_DIR}" in
    ""|/|"${HOME}") die "unsafe installation directory: ${INSTALL_DIR}" ;;
  esac
  if [[ -e "${INSTALL_DIR}" || -L "${INSTALL_DIR}" ]]; then
    die "refusing to replace existing path: ${INSTALL_DIR}"
  fi
  for command_name in curl tar find; do
    command -v "${command_name}" >/dev/null 2>&1 \
      || die "required command was not found on PATH: ${command_name}"
  done

  TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/session-watchdog-install.XXXXXX")"
  archive="${TEMP_DIR}/session-watchdog.tar.gz"
  curl --fail --location --silent --show-error --retry 3 \
    --proto '=https' --tlsv1.2 \
    --output "${archive}" "${ARCHIVE_URL}"
  tar -xzf "${archive}" -C "${TEMP_DIR}"

  extracted_dir="${TEMP_DIR}/session-watchdog-main"
  [[ -f "${extracted_dir}/scripts/pluginctl.py" ]] \
    || die "downloaded archive does not contain the expected Session Watchdog files"

  mkdir -p -- "$(dirname -- "${INSTALL_DIR}")"
  mv -- "${extracted_dir}" "${INSTALL_DIR}"
  source_dir="$(cd -- "${INSTALL_DIR}" && pwd -P)"
  printf 'Downloaded Session Watchdog to: %s\n' "${source_dir}"
fi

ENTRYPOINT="${source_dir}/scripts/pluginctl.py"

if [[ ! -f "${ENTRYPOINT}" ]]; then
  die "command entrypoint not found: ${ENTRYPOINT}"
fi

mkdir -p -- "${BIN_DIR}"
chmod u+x -- "${ENTRYPOINT}"

if [[ -e "${LAUNCHER}" && ! -L "${LAUNCHER}" ]]; then
  die "refusing to replace existing non-symlink: ${LAUNCHER}"
fi

ln -sfn -- "${ENTRYPOINT}" "${LAUNCHER}"

printf 'Installed command launcher:\n  %s -> %s\n' "${LAUNCHER}" "${ENTRYPOINT}"

case ":${PATH:-}:" in
  *":${BIN_DIR}:"*)
    printf '\nNext step:\n  session-watchdog-plugin plug-in\n'
    ;;
  *)
    printf '\nWarning: %s is not on PATH.\n' "${BIN_DIR}"
    printf 'Add it to PATH, or run:\n  %s plug-in\n' "${LAUNCHER}"
    ;;
esac
