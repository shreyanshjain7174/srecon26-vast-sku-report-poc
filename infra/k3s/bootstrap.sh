#!/usr/bin/env bash
# This script is intentionally inert until an operator explicitly invokes it on
# a candidate KVM host after the paid-run controller has passed its gates.
set -Eeuo pipefail

readonly EXPECTED_VLLM_DIGEST="sha256:770fe65b2c73ee74a5c42165cf3433de4048cc2cd9c57a937ca4e35aba5aa87b"
readonly ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

require() {
  local name="$1"
  [[ -n "${!name:-}" ]] || { echo "missing required frozen contract input: ${name}" >&2; exit 64; }
}

require CANARY_IMAGE_DIGEST
require CANARY_MODEL
require CANARY_MODEL_REVISION
[[ "$CANARY_IMAGE_DIGEST" == "$EXPECTED_VLLM_DIGEST" ]] || {
  echo "CANARY_IMAGE_DIGEST does not match the approved immutable digest" >&2
  exit 65
}
[[ "$CANARY_MODEL" != "REQUIRED_AT_RUN_TIME" && "$CANARY_MODEL_REVISION" != "REQUIRED_AT_RUN_TIME" ]] || {
  echo "model contract placeholders must be replaced by a frozen run contract" >&2
  exit 65
}
[[ "$CANARY_MODEL" =~ ^[A-Za-z0-9._/@:-]+$ && "$CANARY_MODEL_REVISION" =~ ^[A-Za-z0-9._/@:-]+$ ]] || {
  echo "model contract inputs contain unsupported characters" >&2
  exit 65
}
command -v kubectl >/dev/null || { echo "kubectl is required; refusing bootstrap" >&2; exit 69; }
[[ -r /sys/fs/cgroup/cgroup.controllers ]] || { echo "cgroup v2 is required" >&2; exit 69; }
systemctl --version >/dev/null || { echo "systemd is required" >&2; exit 69; }
nvidia-smi -L >/dev/null || { echo "nvidia-smi is required" >&2; exit 69; }

# k3s and the NVIDIA runtime must already be installed by a separately approved
# host-provisioning step. This script neither creates a provider resource nor
# downloads/install software from the network.
install -D -m 0644 "$ROOT_DIR/nvidia-runtime.toml" /etc/rancher/k3s/containerd/config.toml.tmpl
systemctl restart k3s

render_dir="$(mktemp -d)"
trap 'rm -rf "$render_dir"' EXIT
cp "$ROOT_DIR"/*.yaml "$render_dir/"
for file in "$render_dir"/*.yaml; do
  sed -i.bak \
    -e "s|REQUIRED_AT_RUN_TIME_MODEL|${CANARY_MODEL}|g" \
    -e "s|REQUIRED_AT_RUN_TIME_REVISION|${CANARY_MODEL_REVISION}|g" "$file"
  rm -f "$file.bak"
done
rg -q 'REQUIRED_AT_RUN_TIME' "$render_dir" && { echo "unrendered run contract placeholder" >&2; exit 65; }
kubectl apply --server-side --field-manager=srecon26-canary -f "$render_dir"
