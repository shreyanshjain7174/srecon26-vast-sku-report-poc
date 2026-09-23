from datetime import UTC, datetime

from srecon26_poc.local_hpa import LocalArm, Sample, build_local_bundle


def test_bundle_requires_local_synthetic_provenance(tmp_path):
    sample = Sample(datetime(2026, 9, 23, tzinfo=UTC), 0, 0, 0, 1, 1, "local-synthetic", "hpa")
    bundle = build_local_bundle(tmp_path, LocalArm.CPU, [sample])
    assert (bundle / "samples.json").exists()
    assert (bundle / "metadata.json").exists()
