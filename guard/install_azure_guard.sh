#!/usr/bin/env bash
# Install reviewed code and non-secret metadata only. No credential argument.
set -euo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

refuse() { echo 'Azure guard installation refused: unsafe prerequisite or permissions' >&2; exit 2; }
[[ ${EUID} -eq 0 && $# -eq 1 ]] || refuse
[[ $(uname -s) == Linux ]] || refuse
/usr/bin/python3 -I - <<'PY' || refuse
import pathlib
import sys
release = dict(line.split('=', 1) for line in pathlib.Path('/etc/os-release').read_text().splitlines() if '=' in line)
if release.get('ID', '').strip('"') != 'ubuntu' or sys.version_info < (3, 10):
    raise SystemExit(2)
PY
command -v systemctl >/dev/null || refuse

# GNU stat is native on Ubuntu. Check every existing ancestor, not just the
# leaf, before reading root-executed code or installing through a directory.
safe_path() {
    local candidate=$1 permissions owner
    [[ ${candidate} == /* && ${candidate} != *'/../'* && ${candidate} != *'/./'* ]] || refuse
    while [[ ${candidate} != / ]]; do
        if [[ -e ${candidate} || -L ${candidate} ]]; then
            [[ ! -L ${candidate} ]] || refuse
            owner=$(stat -c '%u' -- "${candidate}") || refuse
            permissions=$(stat -c '%a' -- "${candidate}") || refuse
            [[ ${owner} == 0 ]] || refuse
            (( (8#${permissions} & 0022) == 0 )) || refuse
        fi
        candidate=$(dirname -- "${candidate}")
    done
}

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
metadata_file=$1
safe_path "${metadata_file}"
[[ -f ${metadata_file} && $(stat -c '%a' -- "${metadata_file}") == 600 ]] || refuse
for name in ssh_rpc.py guard_worker.py guardctl.template srecon26-azure-guard.service.template srecon26-azure-guard.timer.template; do
    safe_path "${script_dir}/${name}"
    [[ -f ${script_dir}/${name} ]] || refuse
done

# Validation produces only the approved executable path; metadata is not shell
# evaluated, and no caller-supplied values are substituted into service units.
vast_bin=$(/usr/bin/python3 -I - "${script_dir}" "${metadata_file}" <<'PY'
import pathlib
import sys
sys.path.insert(0, sys.argv[1])
from ssh_rpc import _json, read_private, validate_config
try:
    config = validate_config(_json(read_private(pathlib.Path(sys.argv[2]), 16384)))
except Exception:
    raise SystemExit(2) from None
print(config['vast_bin'])
PY
) || refuse
safe_path "${vast_bin}"
[[ -f ${vast_bin} && -x ${vast_bin} ]] || refuse
safe_path /etc/ssh/ssh_host_ed25519_key.pub
[[ -f /etc/ssh/ssh_host_ed25519_key.pub ]] || refuse

for target in /var/lib/srecon26-guard /etc/srecon26-guard /usr/local/libexec/srecon26-guard /usr/local/sbin/guardctl /etc/systemd/system/srecon26-azure-guard.service /etc/systemd/system/srecon26-azure-guard.timer; do
    safe_path "${target}"
done
for target in /var/lib/srecon26-guard /etc/srecon26-guard; do
    [[ ! -e ${target} || ( -d ${target} && $(stat -c '%a' -- "${target}") == 700 ) ]] || refuse
done
for name in ssh_rpc.py guard_worker.py; do
    safe_path "/usr/local/libexec/srecon26-guard/${name}"
done
safe_path /etc/srecon26-guard/controller.json
safe_path /etc/srecon26-guard/vast-api-key
if [[ -e /etc/srecon26-guard/vast-api-key ]]; then
    [[ -f /etc/srecon26-guard/vast-api-key && $(stat -c '%a' /etc/srecon26-guard/vast-api-key) == 600 ]] || refuse
fi

install -d -o root -g root -m 0700 /var/lib/srecon26-guard /etc/srecon26-guard
install -d -o root -g root -m 0755 /usr/local/libexec/srecon26-guard
install -d -o root -g root -m 0755 /usr/local/sbin
install -o root -g root -m 0644 "${script_dir}/ssh_rpc.py" /usr/local/libexec/srecon26-guard/ssh_rpc.py
install -o root -g root -m 0644 "${script_dir}/guard_worker.py" /usr/local/libexec/srecon26-guard/guard_worker.py
install -o root -g root -m 0755 "${script_dir}/guardctl.template" /usr/local/sbin/guardctl
install -o root -g root -m 0600 "${metadata_file}" /etc/srecon26-guard/controller.json
install -o root -g root -m 0644 "${script_dir}/srecon26-azure-guard.service.template" /etc/systemd/system/srecon26-azure-guard.service
install -o root -g root -m 0644 "${script_dir}/srecon26-azure-guard.timer.template" /etc/systemd/system/srecon26-azure-guard.timer
systemctl daemon-reload
systemctl enable --now srecon26-azure-guard.timer
systemctl is-enabled --quiet srecon26-azure-guard.timer
systemctl is-active --quiet srecon26-azure-guard.timer
echo 'Azure guard code and timer installed; bootstrap credential separately before arming.'
