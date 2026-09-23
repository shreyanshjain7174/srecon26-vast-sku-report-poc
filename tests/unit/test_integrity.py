from datetime import UTC, datetime

import pytest

from srecon26_poc.integrity import IntegrityError, build_integrity_bundle, validate_integrity_bundle


def test_complete_bundle_is_checksummed_root_hashed_and_anchored(tmp_path):
    bundle = tmp_path / "bundle"; bundle.mkdir()
    (bundle / "samples.json").write_text('{"provenance":"local-synthetic","timestamp":"2026-09-23T00:00:00+00:00"}')
    (bundle / "metadata.json").write_text('{"provenance":"local-synthetic","events":[1,2]}')
    root = build_integrity_bundle(bundle, ["samples.json", "metadata.json"])
    validate_integrity_bundle(bundle, ["samples.json", "metadata.json"], root)
    assert (bundle / "SHA256SUMS").exists() and (bundle / "ROOT-HASH.txt").exists()


def test_missing_or_wrong_receipt_fails_closed(tmp_path):
    bundle = tmp_path / "bundle"; bundle.mkdir()
    (bundle / "samples.json").write_text('{"provenance":"local-synthetic"}')
    root = build_integrity_bundle(bundle, ["samples.json"])
    with pytest.raises(IntegrityError):
        validate_integrity_bundle(bundle, ["samples.json", "metadata.json"], root)
    with pytest.raises(IntegrityError):
        validate_integrity_bundle(bundle, ["samples.json"], "wrong")
