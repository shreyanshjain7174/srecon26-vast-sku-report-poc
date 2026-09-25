#!/usr/bin/env bash
#
# The remote half of the SRECon26 canary.  It is intentionally not an SSH
# wrapper and it never creates a provider instance.  Copy this script and an
# exact, checksummed manifest bundle to an already-created Ubuntu 22.04 KVM
# host, then invoke one explicit subcommand over SSH.
#
# The script deliberately has no default action: `bash remote_host_canary.sh`
# only prints usage.  In particular it does not fetch software, change a
# firewall, or open a public port merely because it was copied to a host.

set -Eeuo pipefail
umask 077

readonly K3S_VERSION="v1.36.4+k3s1"
readonly K3S_RELEASE_URL="https://github.com/k3s-io/k3s/releases/download/v1.36.4%2Bk3s1/k3s"
readonly K3S_AMD64_SHA256="835873f37245fc615f547a2fe2af9402a347875f13fa64a1f136de644955ea3f"
readonly NAMESPACE="srecon26-canary"
readonly FIELD_MANAGER="srecon26-remote-canary"
readonly DEFAULT_WAIT_SECONDS=600
readonly DEFAULT_COMMAND_TIMEOUT_SECONDS=60
readonly DEFAULT_LOAD_SECONDS=45
readonly DEFAULT_LOAD_CONCURRENCY=8
readonly DEFAULT_LOAD_PROMPT_REPETITIONS=512
readonly DEFAULT_LOAD_MAX_TOKENS=256
readonly DEFAULT_LOAD_REQUEST_TIMEOUT_SECONDS=90
readonly DEFAULT_INFERENCE_PHASE_SECONDS=1200
readonly DEFAULT_INFERENCE_CONCURRENCY=2
readonly DEFAULT_INFERENCE_PROMPT_REPETITIONS=256
readonly DEFAULT_INFERENCE_MAX_TOKENS=96
readonly DEFAULT_INFERENCE_REQUEST_TIMEOUT_SECONDS=120
readonly DEFAULT_INFERENCE_MAX_MODEL_LEN=4096
readonly INFERENCE_CONTAINER_CLEANUP_RESERVE_SECONDS=15
# Preserve the controller's report, exact teardown, and absence-proof budget.
# The remote phase is never allowed to consume this immutable margin.
readonly MIN_HARD_DEADLINE_MARGIN_SECONDS=420
readonly DEFAULT_HARD_DEADLINE_MARGIN_SECONDS=420

usage() {
  cat <<'USAGE'
Usage: remote_host_canary.sh <probe|install|install-tunnel|install-agent|verify-two-node|deploy|collect|cleanup|inference-smoke>

This program is inert until one of the listed subcommands is supplied.

Required for probe/collect: CANARY_EVIDENCE_DIR (absolute, empty or new dir)
Required for install: K3S_BINARY_PATH (pre-staged local amd64 binary)
Required for install-agent: K3S_BINARY_PATH, NVIDIA_RUNTIME_TEMPLATE,
  CANARY_K3S_SERVER_URL (https URL on port 6443), and CANARY_K3S_TOKEN_FILE
  (pre-staged local mode-0600 token file).  The token stays in that file and
  is never passed in arguments, environment evidence, or logs.
Required for install-tunnel: CANARY_K3S_TUNNEL_HOST, CANARY_K3S_TUNNEL_PORT,
  CANARY_K3S_TUNNEL_USER, CANARY_K3S_TUNNEL_IDENTITY_FILE, and
  CANARY_K3S_TUNNEL_KNOWN_HOSTS.  It exposes server API only on worker
  loopback through a pinned-host-key SSH tunnel.
Required for verify-two-node: CANARY_EVIDENCE_DIR and CANARY_EXPECTED_NODE_NAMES
  as server,worker. It requires exactly two expected Ready GPU-labelled nodes
  before any workload deploy.
Required for deploy/cleanup: CANARY_EVIDENCE_DIR, CANARY_MANIFEST_DIR,
  CANARY_MANIFEST_SHA256.  Deploy additionally needs CANARY_VLLM_IMAGE,
  CANARY_MODEL, CANARY_MODEL_REVISION, and CANARY_HARD_DEADLINE (an RFC3339
  UTC deadline).  Deploy runs one bounded, concurrent localhost-only load
  phase and preserves at least 420 seconds for report, teardown, and absence
  proof.

Required for inference-smoke: CANARY_EVIDENCE_DIR, CANARY_VLLM_IMAGE pinned
by immutable SHA256 digest, CANARY_MODEL, CANARY_MODEL_REVISION, and
CANARY_HARD_DEADLINE.  It starts one direct Docker vLLM server bound only to
127.0.0.1, performs bounded streamed inference, captures raw timing/GPU
evidence, then stops that container.  It never installs or starts k3s and
never creates or destroys a provider instance.

The pinned k3s source is recorded in this script, but installation never
downloads it.  The supplied binary must verify to the recorded SHA256.
USAGE
}

die() {
  printf 'remote_host_canary: %s\n' "$*" >&2
  exit 64
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command is unavailable: $1"
}

require_root() {
  [[ "$(id -u)" -eq 0 ]] || die "this subcommand must run as root on the remote host"
}

require_absolute_dir() {
  local value_name="$1"
  local value="${!value_name:-}"
  [[ "$value" == /* ]] || die "$value_name must be an absolute path"
  mkdir -p -- "$value"
  [[ -d "$value" && ! -L "$value" ]] || die "$value_name must be a real directory"
}

evidence_dir() {
  require_absolute_dir CANARY_EVIDENCE_DIR
  printf '%s\n' "$CANARY_EVIDENCE_DIR"
}

positive_integer() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]] || die "expected a positive integer, got: $1"
}

bounded_wait_seconds() {
  local value="${CANARY_WAIT_SECONDS:-$DEFAULT_WAIT_SECONDS}"
  positive_integer "$value"
  (( value <= 900 )) || die "CANARY_WAIT_SECONDS must be no greater than 900"
  printf '%s\n' "$value"
}

command_timeout_seconds() {
  local value="${CANARY_COMMAND_TIMEOUT_SECONDS:-$DEFAULT_COMMAND_TIMEOUT_SECONDS}"
  positive_integer "$value"
  (( value <= 120 )) || die "CANARY_COMMAND_TIMEOUT_SECONDS must be no greater than 120"
  printf '%s\n' "$value"
}

bounded_value() {
  local variable_name="$1"
  local default_value="$2"
  local maximum="$3"
  local value="${!variable_name:-$default_value}"
  positive_integer "$value"
  (( value <= maximum )) || die "$variable_name must be no greater than $maximum"
  printf '%s\n' "$value"
}

require_local_tcp_port() {
  local variable_name="$1"
  local value="$2"
  [[ "$value" =~ ^[0-9]+$ ]] && (( 10#$value >= 1024 && 10#$value <= 65535 )) \
    || die "$variable_name must be a numeric TCP port between 1024 and 65535"
}

load_seconds() {
  bounded_value CANARY_LOAD_SECONDS "$DEFAULT_LOAD_SECONDS" 120
}

load_concurrency() {
  bounded_value CANARY_LOAD_CONCURRENCY "$DEFAULT_LOAD_CONCURRENCY" 32
}

load_prompt_repetitions() {
  bounded_value CANARY_LOAD_PROMPT_REPETITIONS "$DEFAULT_LOAD_PROMPT_REPETITIONS" 4096
}

load_max_tokens() {
  bounded_value CANARY_LOAD_MAX_TOKENS "$DEFAULT_LOAD_MAX_TOKENS" 1024
}

load_request_timeout_seconds() {
  bounded_value CANARY_LOAD_REQUEST_TIMEOUT_SECONDS "$DEFAULT_LOAD_REQUEST_TIMEOUT_SECONDS" 120
}

inference_phase_seconds() {
  local value
  value="$(bounded_value CANARY_INFERENCE_PHASE_SECONDS "$DEFAULT_INFERENCE_PHASE_SECONDS" 1800)"
  (( value > INFERENCE_CONTAINER_CLEANUP_RESERVE_SECONDS )) \
    || die "CANARY_INFERENCE_PHASE_SECONDS must leave time to stop the direct inference container"
  printf '%s\n' "$value"
}

inference_concurrency() {
  bounded_value CANARY_INFERENCE_CONCURRENCY "$DEFAULT_INFERENCE_CONCURRENCY" 4
}

inference_prompt_repetitions() {
  bounded_value CANARY_INFERENCE_PROMPT_REPETITIONS "$DEFAULT_INFERENCE_PROMPT_REPETITIONS" 2048
}

inference_max_tokens() {
  bounded_value CANARY_INFERENCE_MAX_TOKENS "$DEFAULT_INFERENCE_MAX_TOKENS" 512
}

inference_request_timeout_seconds() {
  bounded_value CANARY_INFERENCE_REQUEST_TIMEOUT_SECONDS "$DEFAULT_INFERENCE_REQUEST_TIMEOUT_SECONDS" 180
}

inference_max_model_len() {
  bounded_value CANARY_INFERENCE_MAX_MODEL_LEN "$DEFAULT_INFERENCE_MAX_MODEL_LEN" 32768
}

hard_deadline_epoch() {
  local hard_deadline="${CANARY_HARD_DEADLINE:-}"
  [[ -n "$hard_deadline" ]] || die "CANARY_HARD_DEADLINE must be an RFC3339 UTC timestamp"
  require_command date
  local epoch
  epoch="$(date -u -d "$hard_deadline" +%s 2>/dev/null)" \
    || die "CANARY_HARD_DEADLINE must be an RFC3339 UTC timestamp"
  [[ "$epoch" =~ ^[0-9]+$ ]] || die "CANARY_HARD_DEADLINE could not be converted to an epoch"
  (( epoch > $(date -u +%s) )) || die "CANARY_HARD_DEADLINE has already elapsed"
  printf '%s\n' "$epoch"
}

hard_deadline_margin_seconds() {
  local value="${CANARY_HARD_DEADLINE_MARGIN_SECONDS:-$DEFAULT_HARD_DEADLINE_MARGIN_SECONDS}"
  positive_integer "$value"
  (( value >= MIN_HARD_DEADLINE_MARGIN_SECONDS && value <= 600 )) \
    || die "CANARY_HARD_DEADLINE_MARGIN_SECONDS must be between $MIN_HARD_DEADLINE_MARGIN_SECONDS and 600"
  printf '%s\n' "$value"
}

remaining_seconds_until() {
  local deadline_epoch="$1"
  local now
  now="$(date -u +%s)"
  (( deadline_epoch > now )) || return 1
  printf '%s\n' "$(( deadline_epoch - now ))"
}

pressure_phase_deadline_epoch() {
  local hard_deadline margin duration now latest_safe_phase_deadline phase_deadline
  hard_deadline="$(hard_deadline_epoch)"
  margin="$(hard_deadline_margin_seconds)"
  duration="$(load_seconds)"
  now="$(date -u +%s)"
  latest_safe_phase_deadline="$(( hard_deadline - margin ))"
  (( latest_safe_phase_deadline > now && latest_safe_phase_deadline - now >= duration )) \
    || die "CANARY_HARD_DEADLINE does not leave enough time for the bounded load phase and teardown margin"
  phase_deadline="$(( now + duration ))"
  printf '%s\n' "$phase_deadline"
}

inference_phase_deadline_epoch() {
  local hard_deadline margin duration now latest_safe_phase_deadline phase_deadline
  hard_deadline="$(hard_deadline_epoch)"
  margin="$(hard_deadline_margin_seconds)"
  duration="$(inference_phase_seconds)"
  now="$(date -u +%s)"
  latest_safe_phase_deadline="$(( hard_deadline - margin ))"
  (( latest_safe_phase_deadline > now && latest_safe_phase_deadline - now >= duration )) \
    || die "CANARY_HARD_DEADLINE does not leave enough time for the bounded direct inference phase and teardown margin"
  # Hold back local container shutdown time inside the bounded phase.  The
  # resulting deadline is still before the immutable external teardown margin.
  phase_deadline="$(( now + duration - INFERENCE_CONTAINER_CLEANUP_RESERVE_SECONDS ))"
  printf '%s\n' "$phase_deadline"
}

# Evidence output must never intentionally include credentials.  Command
# output is redacted before it is persisted; this is defense in depth because
# the script does not read environment variables or Kubernetes Secrets.
redact_stream() {
  sed -E \
    -e 's/(Authorization: Bearer )[A-Za-z0-9._~+\/-]+/\1[REDACTED]/Ig' \
    -e 's/((api[_-]?key|token|password|secret)[=:][[:space:]]*)[^[:space:]]+/\1[REDACTED]/Ig'
}

capture() {
  local output="$1"
  shift
  local timeout_seconds
  timeout_seconds="$(command_timeout_seconds)"
  # shellcheck disable=SC2068 # command arguments intentionally preserved
  if timeout --foreground "$timeout_seconds" "$@" 2>&1 | redact_stream >"$output"; then
    return 0
  fi
  local result=${PIPESTATUS[0]}
  printf 'command exited %s\n' "$result" >>"$output"
  return "$result"
}

write_json_status() {
  local destination="$1"
  local status="$2"
  local detail="$3"
  require_command jq
  jq -n --arg status "$status" --arg detail "$detail" \
    --arg k3s_version "$K3S_VERSION" --arg k3s_release_url "$K3S_RELEASE_URL" \
    '{status: $status, detail: $detail, k3s_version: $k3s_version, k3s_release_url: $k3s_release_url}' \
    >"$destination"
}

host_is_ubuntu_2204() {
  [[ -r /etc/os-release ]] || return 1
  # shellcheck disable=SC1091
  . /etc/os-release
  [[ "${ID:-}" == "ubuntu" && "${VERSION_ID:-}" == "22.04" ]]
}

probe_host() {
  require_root
  require_command timeout
  require_command systemctl
  require_command nvidia-smi
  require_command nvidia-container-runtime
  require_command docker
  require_command jq
  local out
  out="$(evidence_dir)"

  capture "$out/os-release.txt" cat /etc/os-release || { write_json_status "$out/probe-status.json" "FAILED" "cannot read os-release"; die "cannot read os-release"; }
  host_is_ubuntu_2204 || { write_json_status "$out/probe-status.json" "FAILED" "Ubuntu 22.04 is required"; die "Ubuntu 22.04 is required"; }
  capture "$out/systemd.txt" systemctl --version || { write_json_status "$out/probe-status.json" "FAILED" "systemd is unavailable"; die "systemd is unavailable"; }
  capture "$out/systemd-state.txt" systemctl is-system-running || {
    # Degraded systemd is a capability failure for this intentionally small canary.
    write_json_status "$out/probe-status.json" "FAILED" "systemd is not running"; die "systemd is not running";
  }
  [[ -r /sys/fs/cgroup/cgroup.controllers ]] || { write_json_status "$out/probe-status.json" "FAILED" "cgroup v2 is required"; die "cgroup v2 is required"; }
  capture "$out/cgroup-v2.txt" cat /sys/fs/cgroup/cgroup.controllers || { write_json_status "$out/probe-status.json" "FAILED" "cannot read cgroup v2 controllers"; die "cannot read cgroup v2 controllers"; }
  capture "$out/kvm-virt.txt" systemd-detect-virt --vm || { write_json_status "$out/probe-status.json" "FAILED" "KVM virtualization probe failed"; die "KVM virtualization probe failed"; }
  grep -qx 'kvm' "$out/kvm-virt.txt" || { write_json_status "$out/probe-status.json" "FAILED" "host is not KVM"; die "host is not KVM"; }
  printf 'virtualization=kvm root_uid=%s nested_kvm_not_required=true\n' "$(id -u)" >"$out/kvm-privilege.txt"
  capture "$out/nvidia-smi.txt" nvidia-smi -L || { write_json_status "$out/probe-status.json" "FAILED" "nvidia-smi GPU probe failed"; die "nvidia-smi GPU probe failed"; }
  capture "$out/cuda.txt" nvidia-smi || { write_json_status "$out/probe-status.json" "FAILED" "CUDA driver probe failed"; die "CUDA driver probe failed"; }
  grep -q 'CUDA Version:' "$out/cuda.txt" || { write_json_status "$out/probe-status.json" "FAILED" "CUDA version was not reported"; die "CUDA version was not reported"; }
  capture "$out/docker-info.txt" docker info || { write_json_status "$out/probe-status.json" "FAILED" "Docker probe failed"; die "Docker probe failed"; }
  capture "$out/nvidia-runtime.txt" nvidia-container-runtime --version || { write_json_status "$out/probe-status.json" "FAILED" "NVIDIA container runtime probe failed"; die "NVIDIA container runtime probe failed"; }
  write_json_status "$out/probe-status.json" "PASSED" "Ubuntu 22.04, systemd, cgroup v2, KVM, NVIDIA/CUDA runtime, and Docker checks passed"
}

verify_k3s_binary() {
  require_root
  local binary="${K3S_BINARY_PATH:-}"
  [[ -n "$binary" && -f "$binary" && ! -L "$binary" ]] || die "K3S_BINARY_PATH must name a regular, pre-staged local file"
  require_command sha256sum
  local actual
  actual="$(sha256sum -- "$binary" | awk '{print $1}')"
  [[ "$actual" == "$K3S_AMD64_SHA256" ]] || die "K3S binary checksum mismatch; refusing unverified install"
  [[ -x "$binary" ]] || die "K3S_BINARY_PATH must be executable"
}

install_k3s() {
  require_root
  require_command install
  require_command ln
  require_command systemctl
  require_command nvidia-container-runtime
  verify_k3s_binary

  local runtime_template="${NVIDIA_RUNTIME_TEMPLATE:-}"
  [[ -n "$runtime_template" && -f "$runtime_template" && ! -L "$runtime_template" ]] || die "NVIDIA_RUNTIME_TEMPLATE must be a regular local file"
  grep -q 'runtimes.nvidia' "$runtime_template" || die "NVIDIA_RUNTIME_TEMPLATE does not configure the NVIDIA runtime"

  install -D -m 0755 -- "$K3S_BINARY_PATH" /usr/local/bin/k3s
  local actual_version
  actual_version="$(/usr/local/bin/k3s --version | awk 'NR == 1 {print $3}')"
  [[ "$actual_version" == "$K3S_VERSION" ]] || die "pre-staged k3s binary version does not match $K3S_VERSION"
  ln -sfn /usr/local/bin/k3s /usr/local/bin/kubectl
  install -D -m 0644 -- "$runtime_template" /etc/rancher/k3s/containerd/config.toml.tmpl
  install -d -m 0755 /etc/rancher/k3s
  cat >/etc/rancher/k3s/config.yaml <<'CONFIG'
write-kubeconfig-mode: "0600"
bind-address: "127.0.0.1"
disable:
  - servicelb
  - traefik
node-label:
  - "srecon26.io/vllm-gpu=true"
CONFIG
  cat >/etc/systemd/system/k3s.service <<'UNIT'
[Unit]
Description=Bounded SRECon26 k3s canary node
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
ExecStart=/usr/local/bin/k3s server
KillMode=process
Delegate=yes
LimitNOFILE=1048576
LimitNPROC=infinity
LimitCORE=infinity
TasksMax=infinity
TimeoutStartSec=120
TimeoutStopSec=60
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable --now k3s.service
  local wait_seconds
  wait_seconds="$(bounded_wait_seconds)"
  timeout --foreground "$wait_seconds" bash -c 'until /usr/local/bin/k3s kubectl get nodes >/dev/null 2>&1; do sleep 2; done' \
    || die "k3s did not become ready within bounded wait"
}

install_k3s_tunnel() {
  require_root
  require_command install
  require_command systemctl
  require_command stat
  require_command ssh
  require_command ssh-keygen
  local host="${CANARY_K3S_TUNNEL_HOST:-}"
  [[ "$host" =~ ^[A-Za-z0-9.-]+$ && "$host" != "localhost" ]] || die "CANARY_K3S_TUNNEL_HOST must be a safe hostname or IP address"
  local port="${CANARY_K3S_TUNNEL_PORT:-}"
  [[ "$port" =~ ^[0-9]+$ ]] && (( 10#$port >= 1 && 10#$port <= 65535 )) || die "CANARY_K3S_TUNNEL_PORT must be a TCP port"
  local user="${CANARY_K3S_TUNNEL_USER:-}"
  [[ "$user" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "CANARY_K3S_TUNNEL_USER is invalid"
  local identity_file="${CANARY_K3S_TUNNEL_IDENTITY_FILE:-}"
  [[ "$identity_file" == /* && -f "$identity_file" && ! -L "$identity_file" ]] || die "CANARY_K3S_TUNNEL_IDENTITY_FILE must name a regular local file"
  [[ "$(stat -c '%a' -- "$identity_file")" == "600" ]] || die "CANARY_K3S_TUNNEL_IDENTITY_FILE must have mode 0600"
  local known_hosts="${CANARY_K3S_TUNNEL_KNOWN_HOSTS:-}"
  [[ "$known_hosts" == /* && -f "$known_hosts" && ! -L "$known_hosts" ]] || die "CANARY_K3S_TUNNEL_KNOWN_HOSTS must name a regular local file"
  [[ -s "$known_hosts" ]] || die "CANARY_K3S_TUNNEL_KNOWN_HOSTS must not be empty"
  ssh-keygen -F "[$host]:$port" -f "$known_hosts" >/dev/null || die "CANARY_K3S_TUNNEL_KNOWN_HOSTS lacks exact host fingerprint"

  install -d -m 0700 /etc/srecon26-k3s
  install -m 0600 -- "$identity_file" /etc/srecon26-k3s/server-bridge.key
  install -m 0600 -- "$known_hosts" /etc/srecon26-k3s/server-known-hosts
  cat >/etc/systemd/system/srecon26-k3s-tunnel.service <<UNIT
[Unit]
Description=Private SRECon26 k3s API tunnel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/etc/srecon26-k3s/server-known-hosts -o GlobalKnownHostsFile=/dev/null -i /etc/srecon26-k3s/server-bridge.key -p ${port} -L 127.0.0.1:6443:127.0.0.1:6443 ${user}@${host}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable --now srecon26-k3s-tunnel.service
  local wait_seconds
  wait_seconds="$(bounded_wait_seconds)"
  timeout --foreground "$wait_seconds" bash -c 'until systemctl is-active --quiet srecon26-k3s-tunnel.service; do sleep 2; done' \
    || die "k3s API tunnel did not become active within bounded wait"
}

install_k3s_agent() {
  require_root
  require_command install
  require_command ln
  require_command systemctl
  require_command nvidia-container-runtime
  require_command stat
  require_command chown
  verify_k3s_binary

  local runtime_template="${NVIDIA_RUNTIME_TEMPLATE:-}"
  [[ -n "$runtime_template" && -f "$runtime_template" && ! -L "$runtime_template" ]] || die "NVIDIA_RUNTIME_TEMPLATE must be a regular local file"
  grep -q 'runtimes.nvidia' "$runtime_template" || die "NVIDIA_RUNTIME_TEMPLATE does not configure the NVIDIA runtime"

  local server_url="${CANARY_K3S_SERVER_URL:-}"
  [[ "$server_url" =~ ^https://[A-Za-z0-9.-]+:6443$ ]] || die "CANARY_K3S_SERVER_URL must be an HTTPS host URL on port 6443"
  local run_root="${CANARY_K3S_RUN_ROOT:-}"
  [[ "$run_root" == /* && -d "$run_root" && ! -L "$run_root" ]] || die "CANARY_K3S_RUN_ROOT must be an absolute non-symlink directory"
  [[ "$(stat -c '%a:%u:%g' -- "$run_root")" == "700:0:0" ]] || die "CANARY_K3S_RUN_ROOT must be root-owned mode 0700"
  local token_file="${CANARY_K3S_TOKEN_FILE:-}"
  [[ "$token_file" == /* && -f "$token_file" && ! -L "$token_file" ]] || die "CANARY_K3S_TOKEN_FILE must name a regular local file"
  [[ "$(stat -c '%a' -- "$token_file")" == "600" ]] || die "CANARY_K3S_TOKEN_FILE must have mode 0600"
  [[ "$(stat -c '%u:%g' -- "$token_file")" == "0:0" ]] || die "CANARY_K3S_TOKEN_FILE must be root-owned"
  [[ "$token_file" == "$run_root"/* ]] || die "CANARY_K3S_TOKEN_FILE must be inside CANARY_K3S_RUN_ROOT"
  [[ -s "$token_file" && "$(stat -c '%s' -- "$token_file")" -le 4096 ]] || die "CANARY_K3S_TOKEN_FILE must not be empty or exceed 4096 bytes"
  if systemctl is-active --quiet k3s.service || systemctl is-active --quiet k3s-agent.service || [[ -e /var/lib/rancher/k3s || -e /etc/rancher/k3s/config.yaml ]]; then
    die "existing k3s state is present; refusing to adopt a node"
  fi

  install -D -m 0755 -- "$K3S_BINARY_PATH" /usr/local/bin/k3s
  local actual_version
  actual_version="$(/usr/local/bin/k3s --version | awk 'NR == 1 {print $3}')"
  [[ "$actual_version" == "$K3S_VERSION" ]] || die "pre-staged k3s binary version does not match $K3S_VERSION"
  ln -sfn /usr/local/bin/k3s /usr/local/bin/kubectl
  install -D -m 0644 -- "$runtime_template" /etc/rancher/k3s/containerd/config.toml.tmpl
  install -d -m 0755 /etc/rancher/k3s
  cat >/etc/rancher/k3s/config.yaml <<CONFIG
server: "${server_url}"
token-file: "${token_file}"
node-label:
  - "srecon26.io/vllm-gpu=true"
CONFIG
  chown root:root /etc/rancher/k3s/config.yaml
  chmod 0600 /etc/rancher/k3s/config.yaml
  cat >/etc/systemd/system/k3s-agent.service <<'UNIT'
[Unit]
Description=Bounded SRECon26 k3s GPU worker
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
ExecStart=/usr/local/bin/k3s agent
KillMode=process
Delegate=yes
LimitNOFILE=1048576
LimitNPROC=infinity
LimitCORE=infinity
TasksMax=infinity
TimeoutStartSec=120
TimeoutStopSec=60
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable --now k3s-agent.service
  local wait_seconds
  wait_seconds="$(bounded_wait_seconds)"
  timeout --foreground "$wait_seconds" bash -c 'until systemctl is-active --quiet k3s-agent.service; do sleep 2; done' \
    || die "k3s agent did not become active within bounded wait"
}

verify_two_node_cluster() {
  require_root
  require_command kubectl
  require_command jq
  local out
  out="$(evidence_dir)"
  systemctl is-active --quiet k3s.service || die "k3s server service is not active"
  local expected="${CANARY_EXPECTED_NODE_NAMES:-}"
  [[ "$expected" =~ ^[A-Za-z0-9._-]+,[A-Za-z0-9._-]+$ ]] || die "CANARY_EXPECTED_NODE_NAMES must be two comma-separated node names"
  local first="${expected%%,*}" second="${expected#*,}"
  [[ "$first" != "$second" ]] || die "CANARY_EXPECTED_NODE_NAMES must name distinct nodes"
  kubectl_cmd get nodes -o json >"$out/two-node-join.json" || die "cannot read Kubernetes nodes"
  jq -e --arg first "$first" --arg second "$second" '
    [ .items[]
      | select(.metadata.name == $first or .metadata.name == $second)
      | select(.metadata.labels["srecon26.io/vllm-gpu"] == "true")
      | select(any(.status.conditions[]?; .type == "Ready" and .status == "True"))
      | select(.status.allocatable["nvidia.com/gpu"] == "1")
      | select(.status.addresses | any(.type == "InternalIP" and (.address | length > 0)))
    ] | length == 2
  ' "$out/two-node-join.json" >/dev/null || die "expected exactly two Ready labelled GPU nodes"
  write_json_status "$out/two-node-join-status.json" "PASSED" "two exact Ready GPU nodes joined with InternalIP and one allocatable GPU each"
}

manifest_hash() {
  local directory="$1"
  (
    cd "$directory"
    find . -type f -name '*.yaml' -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | awk '{print $1}'
  )
}

require_manifest_bundle() {
  local directory="${CANARY_MANIFEST_DIR:-}"
  [[ "$directory" == /* && -d "$directory" && ! -L "$directory" ]] || die "CANARY_MANIFEST_DIR must be an absolute non-symlink directory"
  [[ -f "$directory/namespace.yaml" && -f "$directory/vllm.yaml" && -f "$directory/device-plugin.yaml" ]] || die "manifest bundle is incomplete"
  [[ -n "${CANARY_MANIFEST_SHA256:-}" && "$CANARY_MANIFEST_SHA256" =~ ^[a-f0-9]{64}$ ]] || die "CANARY_MANIFEST_SHA256 must be a lowercase SHA256"
  local actual
  actual="$(manifest_hash "$directory")"
  [[ "$actual" == "$CANARY_MANIFEST_SHA256" ]] || die "manifest bundle checksum mismatch"
}

require_frozen_vllm_contract() {
  [[ "${CANARY_VLLM_IMAGE:-}" =~ @sha256:[a-f0-9]{64}$ ]] || die "CANARY_VLLM_IMAGE must be an image pinned by immutable SHA256 digest"
  [[ "${CANARY_VLLM_IMAGE}" != *:*/*:*@* ]] || die "CANARY_VLLM_IMAGE must not include a mutable tag"
  [[ -n "${CANARY_MODEL:-}" && "${CANARY_MODEL:-}" != "REQUIRED_AT_RUN_TIME" ]] || die "CANARY_MODEL must be frozen"
  [[ -n "${CANARY_MODEL_REVISION:-}" && "${CANARY_MODEL_REVISION:-}" != "REQUIRED_AT_RUN_TIME" ]] || die "CANARY_MODEL_REVISION must be frozen"
  [[ "$CANARY_MODEL" =~ ^[A-Za-z0-9._/@:-]+$ && "$CANARY_MODEL_REVISION" =~ ^[A-Za-z0-9._/@:-]+$ ]] || die "model inputs contain unsupported characters"
}

render_manifest_bundle() {
  local source="$1"
  local rendered="$2"
  mkdir -p -- "$rendered"
  cp -- "$source"/*.yaml "$rendered/"
  local file
  for file in "$rendered"/*.yaml; do
    sed -i \
      -e "s|REQUIRED_AT_RUN_TIME_MODEL|$CANARY_MODEL|g" \
      -e "s|REQUIRED_AT_RUN_TIME_REVISION|$CANARY_MODEL_REVISION|g" \
      "$file"
  done
  ! grep -R --fixed-strings 'REQUIRED_AT_RUN_TIME' "$rendered" >/dev/null || die "unrendered frozen contract placeholder"
  grep -R --fixed-strings "$CANARY_VLLM_IMAGE" "$rendered/vllm.yaml" >/dev/null || die "vLLM manifest does not contain the frozen image digest"
  local image
  while IFS= read -r image; do
    [[ "$image" == *@sha256:* ]] || die "tag-only container image found in manifest bundle"
  done < <(grep -hE '^[[:space:]]*image:[[:space:]]*[^[:space:]]+' "$rendered"/*.yaml | sed -E 's/^[[:space:]]*image:[[:space:]]*//')
}

assert_ssh_only_public_listeners() {
  require_command ss
  local out="$1"
  capture "$out/public-listeners.txt" ss -H -lntu || die "cannot inspect listening ports"
  # A listener on a wildcard address is public unless it is SSH.  This script
  # does not modify firewall rules; failing this gate is safer than opening a
  # port.  Loopback and pod/cluster addresses are permitted.
  if awk '$5 ~ /(^\*|0\.0\.0\.0|\[::\]):/ && $5 !~ /:22$/ { found=1 } END { exit(found ? 0 : 1) }' "$out/public-listeners.txt"; then
    die "a non-SSH wildcard listener is present; refusing deploy"
  fi
}

kubectl_cmd() {
  require_command kubectl
  local timeout_seconds
  timeout_seconds="$(command_timeout_seconds)"
  timeout --foreground "$timeout_seconds" kubectl "$@"
}

wait_for_rollout() {
  local kind="$1"
  local name="$2"
  local wait_seconds
  wait_seconds="$(bounded_wait_seconds)"
  kubectl_cmd -n "$NAMESPACE" rollout status "$kind/$name" --timeout="${wait_seconds}s"
}

run_request_pair() {
  local out="$1"
  require_command curl
  require_command jq
  local port="${CANARY_LOCAL_PORT:-18000}"
  require_local_tcp_port CANARY_LOCAL_PORT "$port"
  local endpoint="http://127.0.0.1:${port}/v1/completions"
  local prompt="${CANARY_PROMPT:-Reply with the word ready.}"
  local payload
  payload="$(jq -cn --arg model "$CANARY_MODEL" --arg prompt "$prompt" '{model: $model, prompt: $prompt, max_tokens: 8, temperature: 0}')"
  kubectl_cmd -n "$NAMESPACE" port-forward --address 127.0.0.1 "svc/vllm" "${port}:8000" >"$out/port-forward.txt" 2>&1 &
  local port_forward_pid=$!
  trap 'kill "$port_forward_pid" >/dev/null 2>&1 || true' RETURN
  local wait_seconds
  wait_seconds="$(bounded_wait_seconds)"
  timeout --foreground "$wait_seconds" bash -c "until curl --silent --show-error --fail --max-time 5 http://127.0.0.1:${port}/health >/dev/null; do sleep 2; done" \
    || die "vLLM localhost health check did not become ready within bounded wait"
  curl --silent --show-error --fail --max-time 60 --output /dev/null \
    --write-out '{"request":"warmup","http_code":%{http_code},"time_total_seconds":%{time_total}}\n' \
    --header 'content-type: application/json' --data "$payload" "$endpoint" >"$out/warmup-timing.json"
  curl --silent --show-error --fail --max-time 60 --output /dev/null \
    --write-out '{"request":"measured","http_code":%{http_code},"time_total_seconds":%{time_total}}\n' \
    --header 'content-type: application/json' --data "$payload" "$endpoint" >"$out/measured-timing.json"
  jq -s '{warmup: .[0], measured: .[1]}' "$out/warmup-timing.json" "$out/measured-timing.json" >"$out/request-timing.json"
  kill "$port_forward_pid" >/dev/null 2>&1 || true
  trap - RETURN
}

cleanup_pressure_processes() {
  local pid
  for pid in "$@"; do
    [[ "$pid" =~ ^[1-9][0-9]*$ ]] || continue
    kill "$pid" >/dev/null 2>&1 || true
    wait "$pid" >/dev/null 2>&1 || true
  done
}

capture_until_deadline() {
  local output="$1"
  local deadline_epoch="$2"
  shift 2
  local remaining
  remaining="$(remaining_seconds_until "$deadline_epoch")" \
    || die "load phase reached its deadline before capturing evidence"
  # This is deliberately independent of CANARY_COMMAND_TIMEOUT_SECONDS: a
  # capture may never cross the pressure phase deadline.
  if timeout --foreground "$remaining" "$@" 2>&1 | redact_stream >"$output"; then
    return 0
  fi
  local result=${PIPESTATUS[0]}
  printf 'command exited %s\n' "$result" >>"$output"
  return "$result"
}

curl_timeout_until_deadline() {
  local deadline_epoch="$1"
  local configured_timeout="$2"
  local remaining
  remaining="$(remaining_seconds_until "$deadline_epoch")" \
    || die "load phase reached its deadline before making an HTTP request"
  (( remaining < configured_timeout )) && configured_timeout="$remaining"
  (( configured_timeout >= 1 )) || die "load phase has no time remaining for an HTTP request"
  printf '%s\n' "$configured_timeout"
}

wait_for_local_endpoint_until_deadline() {
  local url="$1"
  local deadline_epoch="$2"
  local description="$3"
  local request_timeout
  while true; do
    request_timeout="$(curl_timeout_until_deadline "$deadline_epoch" 5)"
    if curl --silent --show-error --fail --max-time "$request_timeout" "$url" >/dev/null; then
      return 0
    fi
    sleep 1
  done
  # Kept for shellcheck/control-flow clarity; the deadline helper exits first.
  die "$description did not become ready before the bounded load deadline"
}

capture_pressure_snapshot() {
  local out="$1"
  local phase="$2"
  local prometheus_port="$3"
  local deadline_epoch="$4"
  local query request_timeout
  # CPU comes from resource metrics. Queue, KV, and the actual vLLM TTFT
  # histogram are preserved together as raw Prometheus query output.
  capture_until_deadline "$out/pressure-${phase}-cpu.txt" "$deadline_epoch" \
    kubectl -n "$NAMESPACE" top pods || die "cannot capture ${phase} CPU resource metrics"
  capture_until_deadline "$out/pressure-${phase}-hpa.json" "$deadline_epoch" \
    kubectl -n "$NAMESPACE" get hpa vllm-observer -o json || die "cannot capture ${phase} HPA desired replicas"
  capture_until_deadline "$out/pressure-${phase}-ready.json" "$deadline_epoch" \
    kubectl -n "$NAMESPACE" get deployment vllm -o json || die "cannot capture ${phase} vLLM ready replicas"
  capture_until_deadline "$out/pressure-${phase}-pods.json" "$deadline_epoch" \
    kubectl -n "$NAMESPACE" get pods -l app=vllm -o json || die "cannot capture ${phase} vLLM pod readiness"
  # Snapshot from the selected GPU pod rather than the control-plane host:
  # a two-node scheduler may place vLLM on either machine.  This binds GPU
  # utilization/memory evidence to the serving workload at each phase.
  capture_until_deadline "$out/pressure-${phase}-gpu.csv" "$deadline_epoch" \
    kubectl -n "$NAMESPACE" exec deployment/vllm -- nvidia-smi --query-gpu=timestamp,name,memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits \
    || die "cannot capture ${phase} GPU utilization from the vLLM pod"
  query='vllm:num_requests_waiting or vllm:kv_cache_usage_perc or vllm:time_to_first_token_seconds_count or vllm:time_to_first_token_seconds_sum'
  request_timeout="$(curl_timeout_until_deadline "$deadline_epoch" 15)"
  curl --silent --show-error --fail --max-time "$request_timeout" --get \
    --data-urlencode "query=$query" \
    "http://127.0.0.1:${prometheus_port}/api/v1/query" | redact_stream \
    >"$out/pressure-${phase}-queue-kv-ttft-prometheus.json" \
    || die "cannot capture ${phase} vLLM queue, KV, and TTFT metrics"
}

run_pressure_request() {
  local out="$1"
  local phase="$2"
  local request_number="$3"
  local endpoint="$4"
  local payload="$5"
  local deadline_epoch="$6"
  local request_timeout timing body
  request_timeout="$(curl_timeout_until_deadline "$deadline_epoch" "$(load_request_timeout_seconds)")"
  timing="$out/pressure-${phase}-request-${request_number}-timing.json"
  body="$out/pressure-${phase}-request-${request_number}.ndjson"
  # vLLM streaming lets curl record the first response byte while retaining
  # the full request latency.  The record explicitly names the method so it
  # is not mistaken for an application-side token timestamp.
  if curl --no-buffer --silent --show-error --fail --max-time "$request_timeout" \
    --output "$body" \
    --write-out "{\"request\":${request_number},\"phase\":\"${phase}\",\"ttft_method\":\"curl_time_starttransfer_first_stream_response_byte\",\"ttft_seconds\":%{time_starttransfer},\"latency_seconds\":%{time_total},\"http_code\":%{http_code}}\\n" \
    --header 'content-type: application/json' --data "$payload" "$endpoint" >"$timing"; then
    return 0
  fi
  jq -n --arg phase "$phase" --argjson request "$request_number" \
    --arg status "failed_or_timed_out" \
    '{request: $request, phase: $phase, status: $status}' >"$timing"
  return 1
}

run_pressure_load() {
  require_command curl
  require_command jq
  require_command kubectl
  require_command timeout
  local out="$1"
  local phase_deadline load_duration concurrency prompt_repetitions max_tokens
  phase_deadline="$(pressure_phase_deadline_epoch)"
  load_duration="$(load_seconds)"
  concurrency="$(load_concurrency)"
  prompt_repetitions="$(load_prompt_repetitions)"
  max_tokens="$(load_max_tokens)"
  local vllm_port="${CANARY_LOCAL_PORT:-18000}"
  local prometheus_port="${CANARY_PROMETHEUS_LOCAL_PORT:-19090}"
  require_local_tcp_port CANARY_LOCAL_PORT "$vllm_port"
  require_local_tcp_port CANARY_PROMETHEUS_LOCAL_PORT "$prometheus_port"
  [[ "$vllm_port" != "$prometheus_port" ]] || die "vLLM and Prometheus localhost ports must differ"

  local baseline_prompt="${CANARY_PROMPT:-Reply with the word ready.}"
  local pressure_prompt="$baseline_prompt"
  local repetition
  for ((repetition = 0; repetition < prompt_repetitions; repetition++)); do
    pressure_prompt+=" pressure"
  done
  local baseline_payload pressure_payload
  baseline_payload="$(jq -cn --arg model "$CANARY_MODEL" --arg prompt "$baseline_prompt" \
    '{model: $model, prompt: $prompt, max_tokens: 8, temperature: 0, stream: true}')"
  pressure_payload="$(jq -cn --arg model "$CANARY_MODEL" --arg prompt "$pressure_prompt" --argjson max_tokens "$max_tokens" \
    '{model: $model, prompt: $prompt, max_tokens: $max_tokens, temperature: 0, stream: true}')"

  local port_forward_timeout
  port_forward_timeout="$(remaining_seconds_until "$phase_deadline")" \
    || die "load phase reached its deadline before port forwarding"
  timeout --foreground "$port_forward_timeout" kubectl -n "$NAMESPACE" port-forward --address 127.0.0.1 \
    "svc/vllm" "${vllm_port}:8000" >"$out/pressure-vllm-port-forward.txt" 2>&1 &
  local vllm_pid=$!
  timeout --foreground "$port_forward_timeout" kubectl -n "$NAMESPACE" port-forward --address 127.0.0.1 \
    "svc/prometheus" "${prometheus_port}:9090" >"$out/pressure-prometheus-port-forward.txt" 2>&1 &
  local prometheus_pid=$!
  local -a request_pids=()
  # `die` exits the remote script, so this must be an EXIT trap rather than a
  # function-return trap.  It prevents a failed phase from leaving tunnel or
  # curl processes alive while the independent guard performs teardown.
  trap 'cleanup_pressure_processes "$vllm_pid" "$prometheus_pid" "${request_pids[@]:-}"' EXIT

  wait_for_local_endpoint_until_deadline "http://127.0.0.1:${vllm_port}/health" "$phase_deadline" "vLLM localhost health check"
  wait_for_local_endpoint_until_deadline "http://127.0.0.1:${prometheus_port}/-/ready" "$phase_deadline" "Prometheus localhost health check"
  capture_pressure_snapshot "$out" before "$prometheus_port" "$phase_deadline"
  run_pressure_request "$out" before 0 "http://127.0.0.1:${vllm_port}/v1/completions" "$baseline_payload" "$phase_deadline" \
    || die "baseline vLLM request failed before pressure load"

  local request_number
  for ((request_number = 1; request_number <= concurrency; request_number++)); do
    run_pressure_request "$out" during "$request_number" "http://127.0.0.1:${vllm_port}/v1/completions" "$pressure_payload" "$phase_deadline" &
    request_pids+=("$!")
  done
  # Give concurrent streams a bounded portion of the phase to enter vLLM's
  # scheduler before taking the pressure snapshot.
  local snapshot_delay=2
  (( load_duration < snapshot_delay )) && snapshot_delay="$load_duration"
  local remaining_before_snapshot
  remaining_before_snapshot="$(remaining_seconds_until "$phase_deadline")" \
    || die "load phase reached its deadline before the during-pressure snapshot"
  (( remaining_before_snapshot < snapshot_delay )) && snapshot_delay="$remaining_before_snapshot"
  sleep "$snapshot_delay"
  capture_pressure_snapshot "$out" during "$prometheus_port" "$phase_deadline"

  local request_failures=0 request_pid
  for request_pid in "${request_pids[@]}"; do
    wait "$request_pid" || request_failures=$(( request_failures + 1 ))
  done
  capture_pressure_snapshot "$out" after "$prometheus_port" "$phase_deadline"
  run_pressure_request "$out" after 0 "http://127.0.0.1:${vllm_port}/v1/completions" "$baseline_payload" "$phase_deadline" \
    || request_failures=$(( request_failures + 1 ))

  jq -n --arg status "$([[ "$request_failures" -eq 0 ]] && printf PASSED || printf FAILED)" \
    --arg hard_deadline "$CANARY_HARD_DEADLINE" --argjson phase_deadline_epoch "$phase_deadline" \
    --argjson load_seconds "$load_duration" --argjson concurrency "$concurrency" \
    --argjson prompt_repetitions "$prompt_repetitions" --argjson max_tokens "$max_tokens" \
    --argjson failed_requests "$request_failures" \
    '{status: $status, hard_deadline: $hard_deadline, pressure_phase_deadline_epoch: $phase_deadline_epoch, load_seconds: $load_seconds, concurrency: $concurrency, prompt_repetitions: $prompt_repetitions, max_tokens: $max_tokens, failed_requests: $failed_requests, ttft_method: "curl_time_starttransfer_first_stream_response_byte"}' \
    >"$out/pressure-load-status.json"
  cleanup_pressure_processes "$vllm_pid" "$prometheus_pid" "${request_pids[@]}"
  trap - EXIT
  (( request_failures == 0 )) || die "one or more concurrent pressure requests failed"
}

# The direct smoke deliberately avoids Kubernetes.  It is intended for an
# already-created GPU host where the lifecycle controller owns instance
# teardown.  Every operation below is capped by ``phase_deadline`` which is
# itself strictly before CANARY_HARD_DEADLINE by the immutable teardown margin.
capture_inference_until_deadline() {
  local output="$1"
  local deadline_epoch="$2"
  shift 2
  local remaining
  remaining="$(remaining_seconds_until "$deadline_epoch")" \
    || die "direct inference phase reached its deadline before capturing evidence"
  if timeout --foreground "$remaining" "$@" 2>&1 | redact_stream >"$output"; then
    return 0
  fi
  local result=${PIPESTATUS[0]}
  printf 'command exited %s\n' "$result" >>"$output"
  return "$result"
}

inference_timeout_until_deadline() {
  local deadline_epoch="$1"
  local configured_timeout="$2"
  local remaining
  remaining="$(remaining_seconds_until "$deadline_epoch")" \
    || die "direct inference phase reached its deadline before an HTTP request"
  (( remaining < configured_timeout )) && configured_timeout="$remaining"
  (( configured_timeout >= 1 )) || die "direct inference phase has no time remaining for an HTTP request"
  printf '%s\n' "$configured_timeout"
}

wait_for_inference_endpoint_until_deadline() {
  local url="$1"
  local deadline_epoch="$2"
  local request_timeout
  while true; do
    request_timeout="$(inference_timeout_until_deadline "$deadline_epoch" 5)"
    if curl --silent --show-error --fail --max-time "$request_timeout" "$url" >/dev/null; then
      return 0
    fi
    sleep 1
  done
}

capture_inference_gpu_snapshot() {
  local out="$1"
  local phase="$2"
  local deadline_epoch="$3"
  capture_inference_until_deadline "$out/inference-nvidia-smi-${phase}.csv" "$deadline_epoch" \
    nvidia-smi --query-gpu=timestamp,name,uuid,driver_version,utilization.gpu,utilization.memory,memory.used,memory.total,power.draw --format=csv,noheader,nounits \
    || die "cannot capture ${phase} GPU utilization and memory evidence"
}

capture_inference_public_listeners() {
  local out="$1"
  local deadline_epoch="$2"
  local phase="$3"
  capture_inference_until_deadline "$out/inference-public-listeners-${phase}.txt" "$deadline_epoch" ss -H -lntu \
    || die "cannot inspect direct-inference listening ports"
}

assert_no_new_inference_public_listener() {
  local out="$1"
  local before="$out/inference-public-listeners-before.txt"
  local after="$out/inference-public-listeners-after.txt"
  local baseline current added
  baseline="$(mktemp)"
  current="$(mktemp)"
  added="$(mktemp)"
  trap 'rm -f "$baseline" "$current" "$added"' RETURN
  awk '$5 ~ /(^\*|0\.0\.0\.0|\[::\]):/ {print $1, $5}' "$before" | sort -u >"$baseline"
  awk '$5 ~ /(^\*|0\.0\.0\.0|\[::\]):/ {print $1, $5}' "$after" | sort -u >"$current"
  comm -13 "$baseline" "$current" >"$added"
  cp -- "$added" "$out/inference-new-public-listeners.txt"
  if [[ -s "$added" ]]; then
    die "direct inference introduced a new wildcard listener"
  fi
}

capture_inference_queue_metrics() {
  local out="$1"
  local phase="$2"
  local endpoint="$3"
  local deadline_epoch="$4"
  local request_timeout
  request_timeout="$(inference_timeout_until_deadline "$deadline_epoch" 15)"
  # vLLM's Prometheus endpoint is optional across versions.  Preserve the raw
  # response and a clear availability record, but do not pretend that a build
  # without it supplied queue pressure evidence.
  if curl --silent --show-error --fail --max-time "$request_timeout" "$endpoint/metrics" | redact_stream >"$out/inference-metrics-${phase}.txt"; then
    grep -E '^(vllm(:|_)(num_requests_waiting|num_requests_running|kv_cache_usage_perc)|vllm_(num_requests_waiting|num_requests_running|gpu_cache_usage_perc))' \
      "$out/inference-metrics-${phase}.txt" >"$out/inference-queue-${phase}.txt" || true
    jq -n --arg status "AVAILABLE" --arg endpoint "$endpoint/metrics" \
      '{status: $status, endpoint: $endpoint, note: "raw vLLM metrics retained; queue/KV matching lines are in inference-queue evidence"}' \
      >"$out/inference-queue-${phase}-status.json"
    return 0
  fi
  printf '# vLLM /metrics was unavailable at the localhost-only endpoint\n' >"$out/inference-metrics-${phase}.txt"
  : >"$out/inference-queue-${phase}.txt"
  jq -n --arg status "UNAVAILABLE" --arg endpoint "$endpoint/metrics" \
    '{status: $status, endpoint: $endpoint, note: "vLLM did not expose Prometheus metrics during this phase"}' \
    >"$out/inference-queue-${phase}-status.json"
}

run_inference_stream_request() {
  local out="$1"
  local label="$2"
  local endpoint="$3"
  local payload="$4"
  local deadline_epoch="$5"
  local request_timeout body raw_body raw_timing timing usage stream_model
  request_timeout="$(inference_timeout_until_deadline "$deadline_epoch" "$(inference_request_timeout_seconds)")"
  body="$out/inference-${label}-body.ndjson"
  # Keep unredacted transport bytes outside evidence.  Even a cleanup failure
  # cannot make them part of the collected run bundle, and provider teardown
  # removes the private VM-local temporary file.
  raw_body="$(mktemp "/var/tmp/srecon26-inference-${label}.XXXXXX")"
  raw_timing="$out/inference-${label}-curl-timing.json"
  timing="$out/inference-${label}-timing.json"
  # curl's first response byte is explicitly retained as raw transport timing.
  # The derived TTFT names that limitation instead of implying an application
  # token timestamp.  ``include_usage`` provides server token counts on the
  # final streaming event, from which TPOT and completion throughput follow.
  if ! curl --no-buffer --silent --show-error --fail --max-time "$request_timeout" \
    --output "$raw_body" \
    --write-out "{\"request\":\"${label}\",\"ttft_method\":\"curl_time_starttransfer_first_stream_response_byte\",\"ttft_seconds\":%{time_starttransfer},\"e2e_seconds\":%{time_total},\"http_code\":%{http_code}}\n" \
    --header 'content-type: application/json' --data "$payload" "$endpoint/v1/completions" >"$raw_timing"; then
    jq -n --arg request "$label" --arg status "FAILED_OR_TIMED_OUT" \
      '{request: $request, status: $status}' >"$timing"
    rm -f "$raw_body"
    return 1
  fi
  if ! redact_stream <"$raw_body" >"$body"; then
    rm -f "$raw_body"
    jq -n --arg request "$label" --arg status "REDACTION_FAILED" \
      '{request: $request, status: $status}' >"$timing"
    return 1
  fi
  rm -f "$raw_body"
  usage="$(sed -n 's/^data: //p' "$body" | sed '/^\[DONE\]$/d' | jq -ces 'map(select(.usage? != null) | .usage) | last // empty')" \
    || { jq -n --arg request "$label" --arg status "MISSING_STREAM_USAGE" '{request: $request, status: $status}' >"$timing"; return 1; }
  stream_model="$(sed -n 's/^data: //p' "$body" | sed '/^\[DONE\]$/d' | jq -res 'map(select(.model? != null) | .model) | last // empty')" \
    || { jq -n --arg request "$label" --arg status "MISSING_STREAM_MODEL" '{request: $request, status: $status}' >"$timing"; return 1; }
  jq -n -e --slurpfile raw "$raw_timing" --argjson usage "$usage" --arg requested_model "$CANARY_MODEL" --arg response_model "$stream_model" '
    ($raw[0] + {status: "PASSED", model: $requested_model, response_model: $response_model, usage: $usage})
    | .prompt_tokens = ($usage.prompt_tokens // null)
    | .completion_tokens = ($usage.completion_tokens // null)
    | .total_tokens = ($usage.total_tokens // null)
    | .tpot_seconds = (if (.completion_tokens != null and .completion_tokens >= 2 and .e2e_seconds > .ttft_seconds) then ((.e2e_seconds - .ttft_seconds) / (.completion_tokens - 1)) else null end)
    | .completion_tokens_per_second = (if (.completion_tokens != null and .e2e_seconds > 0) then (.completion_tokens / .e2e_seconds) else null end)
    | .generation_tokens_per_second = (if (.completion_tokens != null and .completion_tokens >= 2 and .e2e_seconds > .ttft_seconds) then ((.completion_tokens - 1) / (.e2e_seconds - .ttft_seconds)) else null end)
    | select(.http_code == 200 and .model == .response_model and .prompt_tokens != null and .completion_tokens != null and .total_tokens != null)
  ' >"$timing" || return 1
}

cleanup_inference_container() {
  local out="$1"
  local container="$2"
  local deadline_epoch="$3"
  local remaining log_timeout stop_timeout
  docker inspect "$container" >/dev/null 2>&1 || return 0
  remaining="$(remaining_seconds_until "$deadline_epoch")" || return 1
  log_timeout="$remaining"
  (( log_timeout > 5 )) && log_timeout=5
  timeout --foreground "$log_timeout" docker logs --tail=500 "$container" 2>&1 | redact_stream >"$out/inference-container-logs.txt" || return 1
  remaining="$(remaining_seconds_until "$deadline_epoch")" || return 1
  stop_timeout="$remaining"
  (( stop_timeout > 10 )) && stop_timeout=10
  timeout --foreground "$stop_timeout" docker stop --time "$stop_timeout" "$container" >>"$out/inference-container-stop.txt" 2>&1
}

run_direct_inference_smoke() {
  require_root
  require_command timeout
  require_command docker
  require_command curl
  require_command jq
  require_command nvidia-smi
  require_command ss
  require_frozen_vllm_contract
  local out
  out="$(evidence_dir)"
  local phase_deadline local_port container max_model_len concurrency prompt_repetitions max_tokens endpoint
  phase_deadline="$(inference_phase_deadline_epoch)"
  local_port="${CANARY_INFERENCE_LOCAL_PORT:-28000}"
  require_local_tcp_port CANARY_INFERENCE_LOCAL_PORT "$local_port"
  container="${CANARY_INFERENCE_CONTAINER_NAME:-srecon26-vllm-inference-smoke}"
  [[ "$container" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{1,62}$ ]] || die "CANARY_INFERENCE_CONTAINER_NAME is invalid"
  max_model_len="$(inference_max_model_len)"
  concurrency="$(inference_concurrency)"
  prompt_repetitions="$(inference_prompt_repetitions)"
  max_tokens="$(inference_max_tokens)"
  endpoint="http://127.0.0.1:${local_port}"
  capture_inference_public_listeners "$out" "$phase_deadline" before
  if docker inspect "$container" >/dev/null 2>&1; then
    die "direct inference container name is already in use; refusing to touch an existing container"
  fi
  if ss -H -ltn "sport = :${local_port}" | grep -q .; then
    die "CANARY_INFERENCE_LOCAL_PORT is already listening; refusing to share an endpoint"
  fi
  jq -n --arg model "$CANARY_MODEL" --arg revision "$CANARY_MODEL_REVISION" \
    --arg image "$CANARY_VLLM_IMAGE" --arg endpoint "$endpoint" --argjson max_model_len "$max_model_len" \
    '{model: $model, model_revision: $revision, image: $image, endpoint: $endpoint, max_model_len: $max_model_len, network_exposure: "published only on 127.0.0.1"}' \
    >"$out/inference-contract.json"
  capture_inference_until_deadline "$out/inference-gpu-identity.txt" "$phase_deadline" nvidia-smi -L \
    || die "cannot capture direct-inference GPU identity"
  capture_inference_until_deadline "$out/inference-cuda.txt" "$phase_deadline" nvidia-smi \
    || die "cannot capture direct-inference CUDA evidence"
  capture_inference_gpu_snapshot "$out" before "$phase_deadline"
  capture_inference_until_deadline "$out/inference-image-pull.txt" "$phase_deadline" docker pull "$CANARY_VLLM_IMAGE" \
    || die "cannot pull the pinned vLLM image before the bounded deadline"
  # Retain the digest attestations in a JSON shape that cannot include an
  # image's arbitrary environment metadata.  Full Docker inspect output can
  # contain image-authored values that are irrelevant to this evidence and
  # should never be persisted as potential credentials.
  capture_inference_until_deadline "$out/inference-image-inspect.json" "$phase_deadline" \
    docker image inspect --format '{{json .RepoDigests}}' "$CANARY_VLLM_IMAGE" \
    || die "cannot inspect the pinned vLLM image"
  jq -e --arg digest "${CANARY_VLLM_IMAGE##*@}" 'map(select(endswith("@" + $digest))) | length > 0' "$out/inference-image-inspect.json" >/dev/null \
    || die "Docker image inspection did not attest the requested immutable vLLM digest"
  capture_inference_until_deadline "$out/inference-container-run.txt" "$phase_deadline" \
    docker run --detach --rm --name "$container" --runtime nvidia \
      --env NVIDIA_VISIBLE_DEVICES=all --env NVIDIA_DRIVER_CAPABILITIES=compute,utility \
      --publish "127.0.0.1:${local_port}:8000" \
      "$CANARY_VLLM_IMAGE" --model "$CANARY_MODEL" --revision "$CANARY_MODEL_REVISION" \
      --served-model-name "$CANARY_MODEL" --host 0.0.0.0 --port 8000 --max-model-len "$max_model_len" \
    || die "cannot start the direct localhost-only vLLM container"
  trap 'cleanup_inference_container "$out" "$container" "$phase_deadline" || true' EXIT
  wait_for_inference_endpoint_until_deadline "$endpoint/health" "$phase_deadline" \
    || die "vLLM direct localhost health check did not become ready before the bounded deadline"
  capture_inference_until_deadline "$out/inference-vllm-models.json" "$phase_deadline" curl --silent --show-error --fail "$endpoint/v1/models" \
    || die "cannot capture the direct vLLM model identity"
  grep -F -- "$CANARY_MODEL" "$out/inference-vllm-models.json" >/dev/null \
    || die "direct vLLM endpoint did not attest the requested model identity"
  capture_inference_until_deadline "$out/inference-container-command.json" "$phase_deadline" \
    docker inspect --format '{{json .Config.Cmd}}' "$container" \
    || die "cannot inspect the direct vLLM command"
  capture_inference_until_deadline "$out/inference-container-port-bindings.json" "$phase_deadline" \
    docker inspect --format '{{json .HostConfig.PortBindings}}' "$container" \
    || die "cannot inspect direct vLLM port bindings"
  jq -en --arg container "$container" --arg image "$CANARY_VLLM_IMAGE" \
    --slurpfile command "$out/inference-container-command.json" \
    --slurpfile ports "$out/inference-container-port-bindings.json" \
    '{container: $container, image: $image, command: $command[0], port_bindings: $ports[0]}' \
    >"$out/inference-container-inspect.json" \
    || die "direct vLLM container inspection was not valid JSON"
  capture_inference_public_listeners "$out" "$phase_deadline" after
  assert_no_new_inference_public_listener "$out"

  local base_prompt="${CANARY_PROMPT:-Reply with the word ready.}"
  local pressure_prompt="$base_prompt"
  local repetition
  for ((repetition = 0; repetition < prompt_repetitions; repetition++)); do
    pressure_prompt+=" pressure"
  done
  local warmup_payload pressure_payload
  warmup_payload="$(jq -cn --arg model "$CANARY_MODEL" --arg prompt "$base_prompt" \
    '{model: $model, prompt: $prompt, max_tokens: 16, temperature: 0, stream: true, stream_options: {include_usage: true}}')"
  pressure_payload="$(jq -cn --arg model "$CANARY_MODEL" --arg prompt "$pressure_prompt" --argjson max_tokens "$max_tokens" \
    '{model: $model, prompt: $prompt, max_tokens: $max_tokens, temperature: 0, stream: true, stream_options: {include_usage: true}}')"
  capture_inference_queue_metrics "$out" before "$endpoint" "$phase_deadline"
  run_inference_stream_request "$out" warmup "$endpoint" "$warmup_payload" "$phase_deadline" \
    || die "direct vLLM warmup streaming request did not return token usage"
  capture_inference_queue_metrics "$out" after-warmup "$endpoint" "$phase_deadline"

  local -a request_pids=()
  local request_number
  for ((request_number = 1; request_number <= concurrency; request_number++)); do
    run_inference_stream_request "$out" "request-${request_number}" "$endpoint" "$pressure_payload" "$phase_deadline" &
    request_pids+=("$!")
  done
  # Leave a short, bounded window for the concurrent streams to enter the
  # scheduler so the during sample can show real GPU and queue pressure.
  local snapshot_delay=2 remaining_before_snapshot
  remaining_before_snapshot="$(remaining_seconds_until "$phase_deadline")" \
    || die "direct inference phase reached its deadline before the during snapshot"
  (( remaining_before_snapshot < snapshot_delay )) && snapshot_delay="$remaining_before_snapshot"
  (( snapshot_delay >= 1 )) || die "direct inference phase has no time for the during snapshot"
  sleep "$snapshot_delay"
  capture_inference_gpu_snapshot "$out" during "$phase_deadline"
  capture_inference_until_deadline "$out/inference-nvidia-compute-during.csv" "$phase_deadline" \
    nvidia-smi --query-compute-apps=pid,process_name,used_gpu_memory,gpu_uuid --format=csv,noheader,nounits \
    || die "cannot capture during direct-inference GPU compute-process evidence"
  capture_inference_queue_metrics "$out" during "$endpoint" "$phase_deadline"
  local request_failures=0 request_pid
  for request_pid in "${request_pids[@]}"; do
    wait "$request_pid" || request_failures=$(( request_failures + 1 ))
  done
  capture_inference_gpu_snapshot "$out" after "$phase_deadline"
  capture_inference_queue_metrics "$out" after "$endpoint" "$phase_deadline"
  cleanup_inference_container "$out" "$container" "$phase_deadline" \
    || die "cannot capture logs and stop the direct vLLM container before the bounded deadline"
  jq -n --arg status "$([[ "$request_failures" -eq 0 ]] && printf PASSED || printf FAILED)" \
    --arg hard_deadline "$CANARY_HARD_DEADLINE" --argjson phase_deadline_epoch "$phase_deadline" \
    --arg model "$CANARY_MODEL" --arg model_revision "$CANARY_MODEL_REVISION" --arg image "$CANARY_VLLM_IMAGE" \
    --argjson concurrency "$concurrency" --argjson prompt_repetitions "$prompt_repetitions" --argjson max_tokens "$max_tokens" \
    --argjson failed_requests "$request_failures" \
    '{status: $status, hard_deadline: $hard_deadline, phase_deadline_epoch: $phase_deadline_epoch, model: $model, model_revision: $model_revision, image: $image, concurrency: $concurrency, prompt_repetitions: $prompt_repetitions, max_tokens: $max_tokens, failed_requests: $failed_requests, ttft_method: "curl_time_starttransfer_first_stream_response_byte", network_exposure: "127.0.0.1 only"}' \
    >"$out/inference-status.json"
  (( request_failures == 0 )) || die "one or more direct vLLM streaming requests failed"
  trap - EXIT
}

deploy_canary() {
  require_root
  require_command timeout
  require_command kubectl
  require_command jq
  require_manifest_bundle
  require_frozen_vllm_contract
  local out
  out="$(evidence_dir)"
  systemctl is-active --quiet k3s.service || die "k3s service is not active"
  assert_ssh_only_public_listeners "$out"
  local rendered
  rendered="$(mktemp -d)"
  trap 'rm -rf "$rendered"' RETURN
  render_manifest_bundle "$CANARY_MANIFEST_DIR" "$rendered"
  kubectl_cmd apply --server-side --field-manager="$FIELD_MANAGER" -f "$rendered"
  wait_for_rollout daemonset nvidia-device-plugin
  wait_for_rollout deployment metrics-server
  wait_for_rollout deployment vllm
  wait_for_rollout deployment prometheus
  wait_for_rollout deployment prometheus-adapter
  run_request_pair "$out"
  run_pressure_load "$out"
  trap - RETURN
  rm -rf "$rendered"
}

collect_evidence() {
  require_root
  require_command kubectl
  require_command curl
  require_command jq
  local out
  out="$(evidence_dir)"
  systemctl is-active --quiet k3s.service || die "k3s service is not active"
  capture "$out/nodes.txt" kubectl get nodes -o wide || die "cannot capture nodes"
  capture "$out/node-describe.txt" kubectl describe nodes || die "cannot capture node GPU allocatable evidence"
  capture "$out/device-plugin.txt" kubectl -n kube-system get daemonset nvidia-device-plugin -o yaml || die "cannot capture NVIDIA device plugin"
  capture "$out/vllm.txt" kubectl -n "$NAMESPACE" get deployment,pods,svc -o wide || die "cannot capture vLLM workload state"
  capture "$out/vllm-logs.txt" kubectl -n "$NAMESPACE" logs deployment/vllm --tail=200 || die "cannot capture vLLM logs"
  capture "$out/resource-metrics.txt" kubectl top nodes || die "resource metrics API is unavailable"
  capture "$out/custom-metrics.txt" kubectl get --raw "/apis/custom.metrics.k8s.io/v1beta1/namespaces/$NAMESPACE/pods/*/vllm_queue_depth" || die "custom metrics API is unavailable"
  capture "$out/hpa.txt" kubectl -n "$NAMESPACE" get hpa vllm-observer -o yaml || die "cannot capture HPA"
  capture "$out/events.txt" kubectl -n "$NAMESPACE" get events --sort-by=.lastTimestamp || die "cannot capture namespace events"
  local prometheus_port="${CANARY_PROMETHEUS_LOCAL_PORT:-19090}"
  require_local_tcp_port CANARY_PROMETHEUS_LOCAL_PORT "$prometheus_port"
  kubectl_cmd -n "$NAMESPACE" port-forward --address 127.0.0.1 "svc/prometheus" "${prometheus_port}:9090" >"$out/prometheus-port-forward.txt" 2>&1 &
  local prometheus_pid=$!
  trap 'kill "$prometheus_pid" >/dev/null 2>&1 || true' RETURN
  local wait_seconds
  wait_seconds="$(bounded_wait_seconds)"
  timeout --foreground "$wait_seconds" bash -c "until curl --silent --show-error --fail --max-time 5 http://127.0.0.1:${prometheus_port}/-/ready >/dev/null; do sleep 2; done" \
    || die "Prometheus localhost health check did not become ready within bounded wait"
  curl --silent --show-error --fail --max-time 30 "http://127.0.0.1:${prometheus_port}/api/v1/targets" | redact_stream >"$out/prometheus-targets.json"
  curl --silent --show-error --fail --max-time 30 --get --data-urlencode 'query=vllm:num_requests_waiting' "http://127.0.0.1:${prometheus_port}/api/v1/query" | redact_stream >"$out/prometheus-metrics.json"
  kill "$prometheus_pid" >/dev/null 2>&1 || true
  trap - RETURN
  write_json_status "$out/collection-status.json" "PASSED" "captured Kubernetes, vLLM, Prometheus, resource/custom metrics, HPA, events, and timing evidence"
}

cleanup_canary() {
  require_root
  require_command kubectl
  require_manifest_bundle
  local out
  out="$(evidence_dir)"
  local wait_seconds
  wait_seconds="$(bounded_wait_seconds)"
  # `delete -f` with --ignore-not-found is idempotent and scopes deletion to
  # the checksummed canary bundle; it does not delete the host or the VM.
  kubectl_cmd delete --ignore-not-found --wait=true --timeout="${wait_seconds}s" -f "$CANARY_MANIFEST_DIR" \
    >"$out/cleanup.txt" 2>&1 || {
      # A namespace disappearing while the remaining objects are deleted is an
      # idempotent outcome.  Record it and let the exact namespace read decide.
      printf 'manifest deletion returned non-zero; checking namespace absence\n' >>"$out/cleanup.txt"
    }
  if kubectl_cmd get namespace "$NAMESPACE" >"$out/namespace-after-cleanup.txt" 2>&1; then
    die "namespace remains after cleanup"
  fi
  write_json_status "$out/cleanup-status.json" "PASSED" "checksummed Kubernetes canary bundle removed"
}

main() {
  [[ "$#" -eq 1 ]] || { usage >&2; exit 64; }
  case "$1" in
    probe) probe_host ;;
    install) install_k3s ;;
    install-tunnel) install_k3s_tunnel ;;
    install-agent) install_k3s_agent ;;
    verify-two-node) verify_two_node_cluster ;;
    deploy) deploy_canary ;;
    collect) collect_evidence ;;
    cleanup) cleanup_canary ;;
    inference-smoke) run_direct_inference_smoke ;;
    -h|--help) usage ;;
    *) usage >&2; exit 64 ;;
  esac
}

main "$@"
