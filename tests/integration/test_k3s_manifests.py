from pathlib import Path
import re


ROOT = Path(__file__).parents[2]
K3S = ROOT / "infra" / "k3s"


def test_every_container_image_is_pinned_to_a_sha256_digest() -> None:
    images = []
    for path in K3S.glob("*.yaml"):
        images.extend(re.findall(r"^\s*image:\s*(\S+)", path.read_text(), re.MULTILINE))
    assert images
    assert all("@sha256:" in image for image in images)


def test_vllm_requests_exactly_one_gpu_and_is_cluster_local() -> None:
    manifest = (K3S / "vllm.yaml").read_text()
    assert "docker.io/vllm/vllm-openai@sha256:df2607b26bdda2875de4832f4d08da0055b4b6e3570347f3a849bcc652771dd6" in manifest
    assert manifest.count("nvidia.com/gpu: \"1\"") == 2
    assert "type: ClusterIP" in manifest
    assert "kind: NetworkPolicy" in manifest
    assert "--revision" in manifest
    assert 'srecon26.io/vllm-gpu: "true"' in manifest
    assert manifest.count("nvidia.com/gpu: \"1\"") == 2
    assert "runAsNonRoot: true" in manifest
    assert "allowPrivilegeEscalation: false" in manifest


def test_metric_path_exposes_queue_and_kv_to_an_autoscaling_v2_hpa() -> None:
    adapter = (K3S / "prometheus-adapter.yaml").read_text()
    hpa = (K3S / "hpa-observer.yaml").read_text()
    prometheus = (K3S / "prometheus.yaml").read_text()
    assert "vllm:num_requests_waiting" in adapter
    assert "vllm:kv_cache_usage_perc" in adapter
    assert "custom.metrics.k8s.io" in adapter
    assert "apiVersion: autoscaling/v2" in hpa
    assert "vllm_queue_depth" in hpa
    assert "vllm_kv_cache_usage_perc" in hpa
    assert 'averageValue: "0.8"' in hpa
    assert "kubernetes_sd_configs:" in prometheus
    assert "__meta_kubernetes_pod_name" in prometheus
    assert "target_label: pod" in prometheus
    assert "target_label: namespace" in prometheus
    assert "serviceAccountName: prometheus" in prometheus
    assert "system:auth-delegator" in adapter
    assert "system:auth-delegator" in (K3S / "metrics-server.yaml").read_text()


def test_bootstrap_rejects_unfrozen_contract_inputs_before_kubernetes_actions() -> None:
    script = (K3S / "bootstrap.sh").read_text()
    assert 'require CANARY_IMAGE_DIGEST' in script
    assert 'require CANARY_MODEL_REVISION' in script
    assert 'CANARY_IMAGE_DIGEST does not match' in script
    assert 'kubectl apply --server-side' in script
