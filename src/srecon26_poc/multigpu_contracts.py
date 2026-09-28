from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, fields
from decimal import Decimal
from typing import Any

MODEL_ID = "Qwen/Qwen3.8-27B"
MODEL_REVISION = "1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0"
MODEL_USED_STORAGE_BYTES = 55615875685
MODEL_PARAMS = 27781427952
VLLM_IMAGE = "docker.io/vllm/vllm-openai@sha256:5f5e535216848d0c52159c8c13a0af04be5f6fe1a84e79914300610796f76d40"

REQUIRED_GPU_COUNT = 8
REQUIRED_TENSOR_PARALLEL = 8
ALLOWED_GPU_NAMES = frozenset({"RTX 4090"})
MIN_GPU_RAM_MIB = 24564
REQUIRED_COMPUTE_CAPABILITY = "8.9"
MIN_RELIABILITY = Decimal("0.99")
MIN_DIRECT_PORTS = 1
MIN_INET_DOWN_MBPS = Decimal("250")
MAX_MODEL_LEN = 4096
MAX_NUM_SEQS = 64
DISK_GIB = 250
MIN_DEADLINE_MINUTES = 90
MAX_DEADLINE_MINUTES = 150


@dataclass(frozen=True, slots=True)
class ModelConfig:
    num_attention_heads: int
    num_key_value_heads: int
    hidden_size: int
    intermediate_size: int
    num_hidden_layers: int


MODEL_CONFIG = ModelConfig(
    num_attention_heads=24,
    num_key_value_heads=4,
    hidden_size=5120,
    intermediate_size=17408,
    num_hidden_layers=64,
)


def validate_tensor_parallel(tp: int, config: ModelConfig) -> None:
    if tp < 1:
        raise ValueError(f"tensor_parallel must be positive, got {tp}")
    for name in ("num_attention_heads", "hidden_size", "intermediate_size"):
        value = getattr(config, name)
        if value % tp:
            raise ValueError(f"{name}={value} not divisible by tensor_parallel={tp}")
    kv = config.num_key_value_heads
    # vLLM shards KV heads when kv % tp == 0, replicates them when tp % kv == 0.
    if kv % tp and tp % kv:
        raise ValueError(f"num_key_value_heads={kv} incompatible with tensor_parallel={tp}")


def _require_int(name: str, value: Any) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be int, got {type(value).__name__}")


def _require_decimal(name: str, value: Any) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal, got {type(value).__name__}")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite, got {value}")


@dataclass(frozen=True, slots=True)
class BenchmarkCell:
    name: str
    input_tokens: int
    output_tokens: int
    concurrency: int
    num_requests: int

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("cell name must be a non-empty string")
        for name in ("input_tokens", "output_tokens", "concurrency", "num_requests"):
            value = getattr(self, name)
            _require_int(name, value)
            if value < 1:
                raise ValueError(f"{name} must be positive, got {value}")
        if self.input_tokens + self.output_tokens > MAX_MODEL_LEN:
            raise ValueError(
                f"input_tokens+output_tokens={self.input_tokens + self.output_tokens} exceeds max_model_len={MAX_MODEL_LEN}"
            )
        if self.concurrency > MAX_NUM_SEQS:
            raise ValueError(f"concurrency={self.concurrency} exceeds max_num_seqs={MAX_NUM_SEQS}")
        if self.num_requests < self.concurrency:
            raise ValueError(f"num_requests={self.num_requests} below concurrency={self.concurrency}")


_SMOKE_CELLS = (
    BenchmarkCell("short-c1", 1024, 256, 1, 32),
    BenchmarkCell("short-c8", 1024, 256, 8, 64),
)
_FULL_CELLS = _SMOKE_CELLS + (
    BenchmarkCell("short-c32", 1024, 256, 32, 128),
    BenchmarkCell("long-c8", 3072, 512, 8, 64),
)
_MATRIX_CELLS: dict[str, tuple[BenchmarkCell, ...]] = {"smoke": _SMOKE_CELLS, "full": _FULL_CELLS}


@dataclass(frozen=True, slots=True)
class BenchmarkMatrix:
    name: str
    cells: tuple[BenchmarkCell, ...]

    def __post_init__(self) -> None:
        expected = _MATRIX_CELLS.get(self.name)
        if expected is None:
            raise ValueError(f"unknown matrix name {self.name!r}")
        if tuple(self.cells) != expected:
            raise ValueError(f"cells do not match fixed {self.name} matrix")
        object.__setattr__(self, "cells", tuple(self.cells))

    @classmethod
    def for_name(cls, name: str) -> BenchmarkMatrix:
        if name not in _MATRIX_CELLS:
            raise ValueError(f"unknown matrix name {name!r}")
        return cls(name=name, cells=_MATRIX_CELLS[name])


_INT_FIELDS = (
    "offer_id",
    "machine_id",
    "gpu_count",
    "gpu_ram_mib",
    "direct_port_count",
    "tensor_parallel",
    "max_model_len",
    "max_num_seqs",
    "disk_gib",
    "deadline_minutes",
)
_DECIMAL_FIELDS = ("dph", "max_dph", "reliability", "inet_down_mbps")


@dataclass(frozen=True, slots=True)
class MultiGpuPlan:
    offer_id: int
    machine_id: int
    gpu_name: str
    gpu_count: int
    gpu_ram_mib: int
    compute_capability: str
    dph: Decimal
    max_dph: Decimal
    reliability: Decimal
    direct_port_count: int
    inet_down_mbps: Decimal
    tensor_parallel: int
    max_model_len: int
    max_num_seqs: int
    disk_gib: int
    deadline_minutes: int
    matrix_name: str
    model_id: str = MODEL_ID
    model_revision: str = MODEL_REVISION
    vllm_image: str = VLLM_IMAGE

    def __post_init__(self) -> None:
        for name in _INT_FIELDS:
            _require_int(name, getattr(self, name))
        for name in _DECIMAL_FIELDS:
            _require_decimal(name, getattr(self, name))

        pins = (("model_id", MODEL_ID), ("model_revision", MODEL_REVISION), ("vllm_image", VLLM_IMAGE))
        for name, expected in pins:
            if getattr(self, name) != expected:
                raise ValueError(f"{name} must be pinned to {expected!r}, got {getattr(self, name)!r}")

        if self.gpu_count != REQUIRED_GPU_COUNT:
            raise ValueError(f"gpu_count must be {REQUIRED_GPU_COUNT}, got {self.gpu_count}")
        if self.tensor_parallel != REQUIRED_TENSOR_PARALLEL:
            raise ValueError(f"tensor_parallel must be {REQUIRED_TENSOR_PARALLEL}, got {self.tensor_parallel}")
        validate_tensor_parallel(self.tensor_parallel, MODEL_CONFIG)

        if self.offer_id < 1:
            raise ValueError(f"offer_id must be positive, got {self.offer_id}")
        if self.machine_id < 1:
            raise ValueError(f"machine_id must be positive, got {self.machine_id}")
        if self.gpu_name not in ALLOWED_GPU_NAMES:
            raise ValueError(f"gpu_name must be one of {sorted(ALLOWED_GPU_NAMES)}, got {self.gpu_name!r}")
        if self.gpu_ram_mib < MIN_GPU_RAM_MIB:
            raise ValueError(f"gpu_ram_mib must be >= {MIN_GPU_RAM_MIB}, got {self.gpu_ram_mib}")
        if self.compute_capability != REQUIRED_COMPUTE_CAPABILITY:
            raise ValueError(
                f"compute_capability must be {REQUIRED_COMPUTE_CAPABILITY}, got {self.compute_capability!r}"
            )
        if not MIN_RELIABILITY <= self.reliability <= 1:
            raise ValueError(f"reliability must be in [{MIN_RELIABILITY}, 1], got {self.reliability}")
        if self.direct_port_count < MIN_DIRECT_PORTS:
            raise ValueError(f"direct_port_count must be >= {MIN_DIRECT_PORTS}, got {self.direct_port_count}")
        if self.inet_down_mbps < MIN_INET_DOWN_MBPS:
            raise ValueError(f"inet_down_mbps must be >= {MIN_INET_DOWN_MBPS}, got {self.inet_down_mbps}")
        if self.max_model_len != MAX_MODEL_LEN:
            raise ValueError(f"max_model_len must be {MAX_MODEL_LEN}, got {self.max_model_len}")
        if self.max_num_seqs != MAX_NUM_SEQS:
            raise ValueError(f"max_num_seqs must be {MAX_NUM_SEQS}, got {self.max_num_seqs}")
        if self.disk_gib != DISK_GIB:
            raise ValueError(f"disk_gib must be {DISK_GIB}, got {self.disk_gib}")
        if not MIN_DEADLINE_MINUTES <= self.deadline_minutes <= MAX_DEADLINE_MINUTES:
            raise ValueError(
                f"deadline_minutes must be in [{MIN_DEADLINE_MINUTES}, {MAX_DEADLINE_MINUTES}], got {self.deadline_minutes}"
            )
        if self.max_dph <= 0:
            raise ValueError(f"max_dph must be positive, got {self.max_dph}")
        if self.dph <= 0:
            raise ValueError(f"dph must be positive, got {self.dph}")
        if self.dph > self.max_dph:
            raise ValueError(f"dph={self.dph} exceeds max_dph={self.max_dph}")
        if self.matrix_name not in _MATRIX_CELLS:
            raise ValueError(f"matrix_name must be one of {sorted(_MATRIX_CELLS)}, got {self.matrix_name!r}")

    @property
    def matrix(self) -> BenchmarkMatrix:
        return BenchmarkMatrix.for_name(self.matrix_name)

    def canonical_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            payload[f.name] = str(value) if isinstance(value, Decimal) else value
        payload["model_config"] = asdict(MODEL_CONFIG)
        payload["model_params"] = MODEL_PARAMS
        payload["model_used_storage_bytes"] = MODEL_USED_STORAGE_BYTES
        payload["matrix"] = {"name": self.matrix_name, "cells": [asdict(c) for c in self.matrix.cells]}
        return payload

    def canonical_json(self) -> str:
        return json.dumps(self.canonical_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()
