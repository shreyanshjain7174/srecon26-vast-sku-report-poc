from __future__ import annotations

import json

import pytest

from srecon26_poc.integrity import IntegrityError
from srecon26_poc.local_evidence import arm_allowlist, finalize_arm_bundle, write_pending_anchor_metadata


def _write_selected_arm(bundle, arm: str) -> None:
    bundle.mkdir()
    for name in arm_allowlist(arm):
        (bundle / name).write_text('{"sample":"local"}\n', encoding="utf-8")


def test_finalize_arm_writes_explicit_allowlist_and_root_hash(tmp_path) -> None:
    bundle = tmp_path / "cpu"
    _write_selected_arm(bundle, "cpu")

    result = finalize_arm_bundle(bundle, "cpu", "phase1-live-example")

    manifest = json.loads((bundle / "EVIDENCE-MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["artifacts"] == list(arm_allowlist("cpu"))
    assert result.root_hash == (bundle / "ROOT-HASH.txt").read_text(encoding="utf-8").strip()
    assert len((bundle / "SHA256SUMS").read_text(encoding="utf-8").splitlines()) == len(arm_allowlist("cpu"))


def test_finalize_arm_rejects_files_outside_explicit_allowlist(tmp_path) -> None:
    bundle = tmp_path / "queue"
    _write_selected_arm(bundle, "queue")
    (bundle / "unreviewed-extra.txt").write_text("not declared\n", encoding="utf-8")

    with pytest.raises(IntegrityError, match="allowlist"):
        finalize_arm_bundle(bundle, "queue", "phase1-live-example")


def test_anchor_metadata_keeps_external_receipt_pending(tmp_path) -> None:
    bundle = tmp_path / "kv"
    _write_selected_arm(bundle, "kv")
    finalized = finalize_arm_bundle(bundle, "kv", "phase1-live-example")
    anchor = tmp_path / "anchor.json"

    write_pending_anchor_metadata(anchor, [finalized], relative_to=tmp_path)

    payload = json.loads(anchor.read_text(encoding="utf-8"))
    assert payload["external_guard_receipt"]["status"] == "PENDING"
    assert payload["external_guard_receipt"]["receipt_path"] is None
    assert payload["external_guard_receipt"]["required_root_hashes"] == {"kv": finalized.root_hash}
    assert payload["selected_arms"][0]["bundle"] == "kv"
