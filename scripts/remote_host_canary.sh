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
# Preserve the controller's report, exact teardown, and absence-proof budget.
# The remote phase is never allowed to consume this immutable margin.
readonly MIN_HARD_DEADLINE_MARGIN_SECONDS=165
readonly DEFAULT_HARD_DEADLINE_MARGIN_SECONDS=180

usage() {
  cat <<'USAGE'
Usage: remote_host_canary.sh <probe|install|deploy|collect|cleanup>

This program is inert until one of the listed subcommands is supplied.

Required for probe/collect: CANARY_EVIDENCE_DIR (absolute, empty or new dir)
Required for install: K3S_BINARY_PATH (pre-staged local amd64 binary)
Required for deploy/cleanup: CANARY_EVIDENCE_DIR, CANARY_MANIFEST_DIR,
  CANARY_MANIFEST_SHA256.  Deploy additionally needs CANARY_VLLM_IMAGE,
  CANARY_MODEL, CANARY_MODEL_REVISION, and CANARY_HARD_DEADLINE (an RFC3339
  UTC deadline).  Deploy runs one bounded, concurrent localhost-only load
  phase and preserves at least 165 seconds for report, teardown, and absence
  proof.

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
  (( value >= MIN_HARD_DEADLINE_MARGIN_SECONDS && value <= 300 )) \
    || die "CANARY_HARD_DEADLINE_MARGIN_SECONDS must be between $MIN_HARD_DEADLINE_MARGIN_SECONDS and 300"
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
  write_json_status "$out/probe-status.json" "PASSED" "Ubuntu 22.04, systemd, cgroup v2, KVM, NVIDIA/CUDA, and Docker checks passed"
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
  install -D -m 0644 -- "$runtime_template" /etc/rancher/k3s/containerd/config.toml.tmpl
  install -d -m 0755 /etc/rancher/k3s
  cat >/etc/rancher/k3s/config.yaml <<'CONFIG'
write-kubeconfig-mode: "0600"
bind-address: "127.0.0.1"
disable:
  - servicelb
  - traefik
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

manifest_hash() {
  local directory="$1"
  find "$directory" -type f -name '*.yaml' -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | awk '{print $1}'
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
  [[ "$port" =~ ^[1-9][0-9]{3,4}$ ]] || die "CANARY_LOCAL_PORT must be a local high TCP port"
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
  [[ "$vllm_port" =~ ^[1-9][0-9]{3,4}$ ]] || die "CANARY_LOCAL_PORT must be a local high TCP port"
  [[ "$prometheus_port" =~ ^[1-9][0-9]{3,4}$ ]] || die "CANARY_PROMETHEUS_LOCAL_PORT must be a local high TCP port"
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
  [[ "$prometheus_port" =~ ^[1-9][0-9]{3,4}$ ]] || die "CANARY_PROMETHEUS_LOCAL_PORT must be a local high TCP port"
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
    deploy) deploy_canary ;;
    collect) collect_evidence ;;
    cleanup) cleanup_canary ;;
    -h|--help) usage ;;
    *) usage >&2; exit 64 ;;
  esac
}

main "$@"
