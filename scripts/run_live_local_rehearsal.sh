#!/usr/bin/env bash
# A single-use Kind rehearsal. It refuses existing/unowned clusters and touches no other context.
set -euo pipefail
ONLY_ARM="${1:-all}"
case "$ONLY_ARM" in all|cpu|queue|kv) ;; *) echo "usage: $0 [all|cpu|queue|kv]" >&2; exit 2;; esac

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_ID="phase1-live-$(date -u +%Y%md%H%M%sz)"
CLUSTER_NAME="srecon26-phase1-live-${RUN_ID#phase1-live-}"
CONTEXT="kind-$CLUSTER_NAME"
OWNER="srecon26-phase1-$RUN_ID"
STATE_DIR="$ROOT_DIR/local-evidence/$RUN_ID"
KUBECONFIG_FILE="$STATE_DIR/kubeconfig"
NAMESPACE="srecon26-local"
IMAGE="srecon26-phase1-mock:local"
CREATED=0
mkdir -p "$STATE_DIR"
exec > >(tee "$STATE_DIR/runner.log") 2>&1

for tool in docker kind kubectl helm jq; do command -v "$tool" >/dev/null || { echo "missing $tool" >&2; exit 1; }; done
docker info >/dev/null
if kind get clusters | grep -qx "$CLUSTER_NAME"; then echo "refusing to reuse cluster $CLUSTER_NAME" >&2; exit 1; fi

k() { kubectl --kubeconfig "$KUBECONFIG_FILE" --context "$CONTEXT" "$@"; }
owned() { test -f "$STATE_DIR/owner" && test "$(cat "$STATE_DIR/owner")" = "$OWNER" && kind get clusters | grep -qx "$CLUSTER_NAME"; }
cleanup() {
  status=$?
  if [ "$CREATED" = 1 ] && owned; then
    kind delete cluster --name "$CLUSTER_NAME" --kubeconfig "$KUBECONFIG_FILE" || true
    printf 'deleted %s at %s\n' "$CLUSTER_NAME" "$(date -u +%FT%TZ)" >> "$STATE_DIR/cleanup.log"
  fi
  exit "$status"
}
trap cleanup EXIT

printf '%s\n' "$OWNER" > "$STATE_DIR/owner"
kind create cluster --name "$CLUSTER_NAME" --wait 120s --kubeconfig "$KUBECONFIG_FILE"
CREATED=1
k get --raw=/readyz >/dev/null
docker build --label "org.srecon26.owner=$OWNER" -t "$IMAGE" "$ROOT_DIR/mock-vllm"
kind load docker-image "$IMAGE" --name "$CLUSTER_NAME"
k apply -f "$ROOT_DIR/k8s/live/base.yaml"
k label namespace "$NAMESPACE" "srecon26.io/owner=$OWNER" --overwrite
k -n "$NAMESPACE" label deployment/hpa-target "srecon26.io/owner=$OWNER" --overwrite
k apply -f "$ROOT_DIR/k8s/live/prometheus.yaml"
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update >/dev/null
helm repo add metrics-server https://kubernetes-sigs.github.io/metrics-server/ --force-update >/dev/null
helm repo update >/dev/null
helm upgrade --install metrics-server metrics-server/metrics-server --kubeconfig "$KUBECONFIG_FILE" --kube-context "$CONTEXT" --namespace kube-system --version 3.13.0 --set 'args[0]=--kubelet-insecure-tls' >/dev/null
helm upgrade --install prometheus-adapter prometheus-community/prometheus-adapter --kubeconfig "$KUBECONFIG_FILE" --kube-context "$CONTEXT" --namespace "$NAMESPACE" --version 5.3.0 --values "$ROOT_DIR/k8s/live/adapter-values.yaml" >/dev/null
k -n "$NAMESPACE" rollout status deployment/hpa-target --timeout=120s
k -n "$NAMESPACE" rollout status deployment/prometheus --timeout=120s
k -n "$NAMESPACE" rollout status deployment/prometheus-adapter --timeout=120s
k -n kube-system rollout status deployment/metrics-server --timeout=120s

pod() { k -n "$NAMESPACE" get pod -l app=hpa-target -o json | jq -r '[.items[] | select(.metadata.deletionTimestamp == null) | select(any(.status.conditions[]?; .type == "Ready" and .status == "True"))][0].metadata.name'; }
source_metrics() { k -n "$NAMESPACE" exec "$(pod)" -- python -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8000/metrics").read().decode())'; }
control() { k -n "$NAMESPACE" exec "$(pod)" -- python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/control?queue=$1&kv=$2').read().decode())" >/dev/null; }
custom() { k get --raw "/apis/custom.metrics.k8s.io/v1beta1/namespaces/$NAMESPACE/pods/$(pod)/$1"; }
cpu() { k get --raw "/apis/metrics.k8s.io/v1beta1/namespaces/$NAMESPACE/pods/$(pod)"; }
cpu_millicores() { cpu | jq -r '.containers[] | select(.name == "server") | .usage.cpu' | python3 -c 'import sys; raw=sys.stdin.read().strip(); print(float(raw[:-1])/1_000_000 if raw.endswith("n") else float(raw.rstrip("m")))'; }
wait_custom() { local metric=$1 value=$2 end=$((SECONDS+90)); while ! custom "$metric" | jq -e --arg value "$value" '.items[0].value == $value' >/dev/null 2>&1; do (( SECONDS < end )) || return 1; sleep 3; done; }
wait_custom_api() { local end=$((SECONDS+120)); until k get --raw /apis/custom.metrics.k8s.io/v1beta1 >/dev/null 2>&1; do (( SECONDS < end )) || return 1; sleep 3; done; }
wait_one() { local end=$((SECONDS+90)); until [ "$(k -n "$NAMESPACE" get hpa hpa-target -o jsonpath='{.status.desiredReplicas}')" = 1 ] && [ "$(k -n "$NAMESPACE" get deployment hpa-target -o jsonpath='{.status.readyReplicas}')" = 1 ]; do (( SECONDS < end )) || return 1; sleep 3; done; }
wait_two() { local end=$((SECONDS+120)); while [ "$(k -n "$NAMESPACE" get deployment hpa-target -o jsonpath='{.status.readyReplicas}')" != 2 ]; do (( SECONDS < end )) || return 1; sleep 3; done; test "$(k -n "$NAMESPACE" get hpa hpa-target -o jsonpath='{.status.desiredReplicas}')" = 2; }
capture() { local dir=$1 step=$2; mkdir -p "$dir"; date -u +%FT%TZ > "$dir/$step.timestamp"; source_metrics > "$dir/$step.source.prom"; cpu > "$dir/$step.cpu.json" || true; custom vllm_num_requests_waiting > "$dir/$step.queue.custom.json" || true; custom vllm_kv_cache_usage > "$dir/$step.kv.custom.json" || true; k -n "$NAMESPACE" get hpa hpa-target -o json > "$dir/$step.hpa.json"; k -n "$NAMESPACE" get deployment hpa-target -o json > "$dir/$step.deployment.json"; k -n "$NAMESPACE" get events --field-selector involvedObject.name=hpa-target -o json > "$dir/$step.events.json"; }
wait_cpu_below() { local ceiling=$1 end=$((SECONDS+90)); while ! awk -v actual="$(cpu_millicores)" -v ceiling="$ceiling" 'BEGIN { exit !(actual <= ceiling) }'; do (( SECONDS < end )) || return 1; sleep 3; done; }
reset() { k -n "$NAMESPACE" delete hpa hpa-target --ignore-not-found >/dev/null; k -n "$NAMESPACE" scale deployment/hpa-target --replicas=1 >/dev/null; k -n "$NAMESPACE" rollout restart deployment/hpa-target >/dev/null; k -n "$NAMESPACE" rollout status deployment/hpa-target --timeout=90s >/dev/null; control 0 0.2; }
negative_control() { local dir=$1; wait_one; for sample in 0 1 2 3 4 5 6; do capture "$dir" "negative-$sample"; test "$(k -n "$NAMESPACE" get hpa hpa-target -o jsonpath='{.status.desiredReplicas}')" = 1; sleep 15; done; }
arm() {
  local name=$1 manifest=$2 signal=$3
  local dir="$STATE_DIR/$name"
  mkdir -p "$dir"; reset; k apply -f "$ROOT_DIR/k8s/live/$manifest"; k -n "$NAMESPACE" label hpa/hpa-target "srecon26.io/owner=$OWNER" --overwrite; sleep 20
  if [ "$name" = queue ]; then wait_custom vllm_num_requests_waiting 0; wait_cpu_below 48; fi
  if [ "$name" = kv ]; then wait_custom vllm_kv_cache_usage 200m; fi
  negative_control "$dir"; capture "$dir" trigger-before
  case "$name" in
    cpu) k -n "$NAMESPACE" exec "$(pod)" -- python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/burn?seconds=100").read()' >/dev/null ;;
    queue) control 4 0.2; wait_custom vllm_num_requests_waiting 4 ;;
    kv) control 0 1.0; wait_custom vllm_kv_cache_usage 1 ;;
  esac
  wait_two; capture "$dir" scaled; printf '{"arm":"%s","provenance":"local-synthetic","owner":"%s","negative_control_seconds":90,"desired_replicas":2,"ready_replicas":2}\n' "$name" "$OWNER" > "$dir/summary.json"
}

wait_custom_api
if [ "$ONLY_ARM" = all ] || [ "$ONLY_ARM" = cpu ]; then arm cpu hpa-cpu.yaml cpu; fi
if [ "$ONLY_ARM" = all ] || [ "$ONLY_ARM" = queue ]; then arm queue hpa-queue.yaml queue; fi
if [ "$ONLY_ARM" = all ] || [ "$ONLY_ARM" = kv ]; then arm kv hpa-kv.yaml kv; fi
k get --raw /apis/custom.metrics.k8s.io/v1beta1 > "$STATE_DIR/custom-metrics-api.json"
k get --raw /apis/metrics.k8s.io/v1beta1 > "$STATE_DIR/resource-metrics-api.json"
printf '{"result":"passed","cluster":"%s","context":"%s","owner":"%s","provenance":"local-synthetic"}\n' "$CLUSTER_NAME" "$CONTEXT" "$OWNER" > "$STATE_DIR/result.json"
