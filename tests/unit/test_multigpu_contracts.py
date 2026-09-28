import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import pytest

from srecon26_poc.multigpu_contracts import (
    MAX_MODEL_LEN,
    MODEL_CONFIG,
    MODEL_ID,
    MODEL_PARAMS,
    MODEL_REVISION,
    MODEL_USED_STORAGE_BYTES,
    VLLM_IMAGE,
    BenchmarkCell,
    BenchmarkMatrix,
    ModelConfig,
    MultiGpuPlan,
    validate_tensor_parallel,
)


def make_plan(**overrides) -> MultiGpuPlan:
    fields = dict(
        offer_id=123456,
        machine_id=7890,
        gpu_name="RTX 4090",
        gpu_count=8,
        gpu_ram_mib=24564,
        compute_capability="8.9",
        dph=Decimal("3.20"),
        max_dph=Decimal("4.00"),
        reliability=Decimal("0.995"),
        direct_port_count=4,
        inet_down_mbps=Decimal("800"),
        tensor_parallel=8,
        max_model_len=4096,
        max_num_seqs=64,
        disk_gib=250,
        deadline_minutes=120,
        matrix_name="smoke",
    )
    fields.update(overrides)
    return MultiGpuPlan(**fields)


def test_pinned_constants():
    assert MODEL_ID == "Qwen/Qwen3.8-27B"
    assert MODEL_REVISION == "1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0"
    assert MODEL_USED_STORAGE_BYTES == 55615875685
    assert MODEL_PARAMS == 27781427952
    assert VLLM_IMAGE == (
        "docker.io/vllm/vllm-openai@sha256:5f5e535216848d0c52159c8c13a0af04be5f6fe1a84e79914300610796f76d40"
    )
    assert MODEL_CONFIG == ModelConfig(
        num_attention_heads=24,
        num_key_value_heads=4,
        hidden_size=5120,
        intermediate_size=17408,
        num_hidden_layers=64,
    )
    assert MAX_MODEL_LEN == 4096


def test_valid_plan_carries_pins_and_is_frozen():
    plan = make_plan()
    assert (plan.model_id, plan.model_revision, plan.vllm_image) == (MODEL_ID, MODEL_REVISION, VLLM_IMAGE)
    with pytest.raises(FrozenInstanceError):
        plan.gpu_count = 4  # type: ignore[misc]


@pytest.mark.parametrize(
    "field,value",
    [
        ("model_id", "Qwen/Qwen3.8-32B"),
        ("model_revision", "main"),
        ("vllm_image", "docker.io/vllm/vllm-openai:latest"),
    ],
)
def test_rejects_unpinned_model_revision_or_image(field, value):
    with pytest.raises(ValueError, match=field):
        make_plan(**{field: value})


@pytest.mark.parametrize("count", [1, 4, 5, 7, 16])
def test_rejects_gpu_count_other_than_eight(count):
    with pytest.raises(ValueError, match="gpu_count"):
        make_plan(gpu_count=count, tensor_parallel=count)


@pytest.mark.parametrize("tp", [1, 4, 5, 16])
def test_rejects_tensor_parallel_other_than_eight(tp):
    with pytest.raises(ValueError, match="tensor_parallel"):
        make_plan(tensor_parallel=tp)


def test_tp8_divisibility_accepted_with_replicated_kv_heads():
    validate_tensor_parallel(8, MODEL_CONFIG)


def test_tp5_rejected_by_head_divisibility():
    with pytest.raises(ValueError, match="num_attention_heads"):
        validate_tensor_parallel(5, MODEL_CONFIG)


@pytest.mark.parametrize(
    "field,value",
    [
        ("hidden_size", 5124),
        ("intermediate_size", 17412),
    ],
)
def test_rejects_hidden_or_intermediate_not_divisible(field, value):
    config = replace(MODEL_CONFIG, **{field: value})
    with pytest.raises(ValueError, match=field):
        validate_tensor_parallel(8, config)


def test_kv_heads_must_divide_or_be_divisible_by_tp():
    validate_tensor_parallel(2, MODEL_CONFIG)
    validate_tensor_parallel(4, MODEL_CONFIG)
    config = replace(MODEL_CONFIG, num_attention_heads=48, num_key_value_heads=6)
    with pytest.raises(ValueError, match="num_key_value_heads"):
        validate_tensor_parallel(8, config)


@pytest.mark.parametrize("name", ["RTX 3090", "RTX 5090", "H100 SXM", "rtx 4090"])
def test_rejects_non_rtx4090(name):
    with pytest.raises(ValueError, match="gpu_name"):
        make_plan(gpu_name=name)


@pytest.mark.parametrize(
    "overrides,match",
    [
        ({"gpu_ram_mib": 24563}, "gpu_ram_mib"),
        ({"compute_capability": "8.6"}, "compute_capability"),
        ({"reliability": Decimal("0.989")}, "reliability"),
        ({"reliability": Decimal("1.01")}, "reliability"),
        ({"direct_port_count": 0}, "direct_port_count"),
        ({"inet_down_mbps": Decimal("249.9")}, "inet_down_mbps"),
        ({"inet_down_mbps": Decimal("NaN")}, "inet_down_mbps"),
        ({"offer_id": 0}, "offer_id"),
        ({"machine_id": -1}, "machine_id"),
        ({"max_model_len": 8192}, "max_model_len"),
        ({"max_num_seqs": 32}, "max_num_seqs"),
        ({"disk_gib": 200}, "disk_gib"),
        ({"deadline_minutes": 89}, "deadline_minutes"),
        ({"deadline_minutes": 151}, "deadline_minutes"),
        ({"matrix_name": "huge"}, "matrix_name"),
    ],
)
def test_rejects_out_of_contract_fields(overrides, match):
    with pytest.raises(ValueError, match=match):
        make_plan(**overrides)


@pytest.mark.parametrize("deadline", [90, 150])
def test_deadline_bounds_inclusive(deadline):
    assert make_plan(deadline_minutes=deadline).deadline_minutes == deadline


def test_accepts_boundary_hardware_values():
    plan = make_plan(reliability=Decimal("0.99"), inet_down_mbps=Decimal("250"), direct_port_count=1)
    assert plan.reliability == Decimal("0.99")


@pytest.mark.parametrize(
    "dph",
    [Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity"), Decimal("4.0001")],
)
def test_rejects_bad_dph(dph):
    with pytest.raises(ValueError, match="dph"):
        make_plan(dph=dph)


def test_dph_equal_to_max_dph_allowed():
    assert make_plan(dph=Decimal("4.00")).dph == Decimal("4.00")


@pytest.mark.parametrize("max_dph", [Decimal("0"), Decimal("NaN"), Decimal("Infinity")])
def test_rejects_bad_max_dph(max_dph):
    with pytest.raises(ValueError, match="max_dph"):
        make_plan(max_dph=max_dph)


@pytest.mark.parametrize("field", ["dph", "max_dph", "reliability", "inet_down_mbps"])
def test_rejects_float_for_decimal_fields(field):
    with pytest.raises(TypeError, match=field):
        make_plan(**{field: 1.0})


@pytest.mark.parametrize("field", ["offer_id", "gpu_count", "tensor_parallel", "disk_gib"])
def test_rejects_bool_for_int_fields(field):
    with pytest.raises(TypeError, match=field):
        make_plan(**{field: True})


def test_canonical_json_is_deterministic_and_hashable():
    plan = make_plan()
    text = plan.canonical_json()
    assert text == make_plan().canonical_json()
    assert plan.sha256() == hashlib.sha256(text.encode("utf-8")).hexdigest()
    payload = json.loads(text)
    assert list(payload) == sorted(payload)
    assert " " not in text.replace("RTX 4090", "")
    assert payload["dph"] == "3.20"
    assert payload["model_id"] == MODEL_ID
    assert payload["model_config"]["num_key_value_heads"] == 4
    assert payload["model_used_storage_bytes"] == MODEL_USED_STORAGE_BYTES
    assert payload["matrix"]["name"] == "smoke"
    assert len(payload["matrix"]["cells"]) == 2


def test_hash_changes_with_any_field():
    base = make_plan().sha256()
    assert make_plan(offer_id=123457).sha256() != base
    assert make_plan(dph=Decimal("3.21")).sha256() != base
    assert make_plan(matrix_name="full").sha256() != base


def test_smoke_matrix_cells():
    matrix = BenchmarkMatrix.for_name("smoke")
    assert matrix.name == "smoke"
    assert [(c.input_tokens, c.output_tokens, c.concurrency, c.num_requests) for c in matrix.cells] == [
        (1024, 256, 1, 32),
        (1024, 256, 8, 64),
    ]


def test_full_matrix_cells():
    matrix = BenchmarkMatrix.for_name("full")
    assert [(c.input_tokens, c.output_tokens, c.concurrency, c.num_requests) for c in matrix.cells] == [
        (1024, 256, 1, 32),
        (1024, 256, 8, 64),
        (1024, 256, 32, 128),
        (3072, 512, 8, 64),
    ]
    assert len({c.name for c in matrix.cells}) == 4


def test_plan_matrix_property():
    assert make_plan(matrix_name="full").matrix == BenchmarkMatrix.for_name("full")


def test_matrix_rejects_unknown_name_and_altered_cells():
    with pytest.raises(ValueError, match="matrix"):
        BenchmarkMatrix.for_name("medium")
    smoke = BenchmarkMatrix.for_name("smoke")
    with pytest.raises(ValueError, match="cells"):
        BenchmarkMatrix(name="smoke", cells=smoke.cells[:1])
    with pytest.raises(ValueError, match="cells"):
        BenchmarkMatrix(name="full", cells=smoke.cells)


def test_cell_rejects_context_over_max_model_len():
    with pytest.raises(ValueError, match="max_model_len"):
        BenchmarkCell(name="too-long", input_tokens=3585, output_tokens=512, concurrency=1, num_requests=1)
    assert BenchmarkCell(name="edge", input_tokens=3584, output_tokens=512, concurrency=1, num_requests=1)


@pytest.mark.parametrize(
    "overrides,match",
    [
        ({"input_tokens": 0}, "input_tokens"),
        ({"output_tokens": 0}, "output_tokens"),
        ({"concurrency": 0}, "concurrency"),
        ({"concurrency": 65}, "concurrency"),
        ({"num_requests": 0}, "num_requests"),
        ({"num_requests": 4, "concurrency": 8}, "num_requests"),
        ({"name": ""}, "name"),
    ],
)
def test_cell_rejects_invalid_values(overrides, match):
    fields = dict(name="c", input_tokens=128, output_tokens=64, concurrency=1, num_requests=1)
    fields.update(overrides)
    with pytest.raises(ValueError, match=match):
        BenchmarkCell(**fields)


def test_cell_and_matrix_are_frozen():
    matrix = BenchmarkMatrix.for_name("smoke")
    with pytest.raises(FrozenInstanceError):
        matrix.name = "full"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        matrix.cells[0].concurrency = 2  # type: ignore[misc]
    assert isinstance(matrix.cells, tuple)
