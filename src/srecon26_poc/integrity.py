from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Sequence


class IntegrityError(ValueError):
    pass


def _safe_paths(bundle: Path, declared: Sequence[str]) -> list[Path]:
    root = bundle.resolve()
    paths = []
    for name in declared:
        path = (bundle / name).resolve()
        if path.parent != root or not path.is_file():
            raise IntegrityError("declared artifact is missing or outside its bundle")
        paths.append(path)
    return paths


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_integrity_bundle(bundle: Path, declared: Sequence[str]) -> str:
    paths = _safe_paths(bundle, declared)
    lines = [f"{_sha(path)}  {path.name}" for path in sorted(paths)]
    sums = "\n".join(lines) + "\n"
    (bundle / "SHA256SUMS").write_text(sums, encoding="utf-8")
    root_hash = hashlib.sha256(sums.encode("utf-8")).hexdigest()
    (bundle / "ROOT-HASH.txt").write_text(root_hash + "\n", encoding="utf-8")
    return root_hash


def validate_integrity_bundle(bundle: Path, declared: Sequence[str], guard_receipt_root: str) -> None:
    paths = _safe_paths(bundle, declared)
    expected = "\n".join(f"{_sha(path)}  {path.name}" for path in sorted(paths)) + "\n"
    sums_path, root_path = bundle / "SHA256SUMS", bundle / "ROOT-HASH.txt"
    if not sums_path.is_file() or sums_path.read_text(encoding="utf-8") != expected:
        raise IntegrityError("checksum manifest is missing or mismatched")
    root_hash = hashlib.sha256(expected.encode("utf-8")).hexdigest()
    if not root_path.is_file() or root_path.read_text(encoding="utf-8").strip() != root_hash or guard_receipt_root != root_hash:
        raise IntegrityError("root hash is not acknowledged by the guard")
    for path in paths:
        if path.suffix == ".json":
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise IntegrityError("invalid declared JSON artifact") from exc
            if payload.get("provenance") != "local-synthetic":
                raise IntegrityError("mixed or absent local evidence provenance")
