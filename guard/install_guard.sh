#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "guard installation must run as root" >&2
  exit 2
fi
if [[ $# -ne 2 ]]; then
  echo "usage: $0 NONCE PROVIDER_SECRET_FILE" >&2
  exit 2
fi

nonce=$1
secret_file=$2
if [[ ! ${nonce} =~ ^[A-Za-z0-9_-]{8,128}$ ]]; then
  echo "nonce must be 8-128 URL-safe characters" >&2
  exit 2
fi
if [[ ! -f ${secret_file} ]] || [[ $(stat -f '%Sp' "${secret_file}") != '-rw-------' ]] || [[ $(stat -f '%u' "${secret_file}") != 0 ]]; then
  echo "provider secret must be a root-owned 0600 regular file" >&2
  exit 2
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
install -d -o root -g root -m 0700 /var/lib/srecon26-guard /etc/srecon26-guard
install -d -o root -g root -m 0755 /usr/local/libexec/srecon26-guard
install -o root -g root -m 0750 "${script_dir}/guard_worker.py" /usr/local/libexec/srecon26-guard/guard_worker.py
install -o root -g root -m 0600 "${secret_file}" /etc/srecon26-guard/vast-api-key
sed "s/@NONCE@/${nonce}/g" "${script_dir}/srecon26-guard.service.template" > "/etc/systemd/system/srecon26-guard-${nonce}.service"
sed "s/@NONCE@/${nonce}/g" "${script_dir}/srecon26-guard.timer.template" > "/etc/systemd/system/srecon26-guard-${nonce}.timer"
chmod 0644 "/etc/systemd/system/srecon26-guard-${nonce}.service" "/etc/systemd/system/srecon26-guard-${nonce}.timer"
systemctl daemon-reload
systemctl enable --now "srecon26-guard-${nonce}.timer"
