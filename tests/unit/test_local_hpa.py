from datetime import UTC, datetime, timedelta

import pytest

from srecon26_poc.local_hpa import LocalArm, Sample, evaluate_arm


def samples_for(arm):
    start = datetime(2026, 9, 23, tzinfo=UTC)
    values = {"cpu": 10, "queue": 10, "kv": 10}
    result = []
    for tick in range(11):
        signals = dict(values)
        if tick >= 7: signals[arm.value] = {"cpu": 100, "queue": 130, "kv": 100}[arm.value]
        result.append(Sample(start + timedelta(seconds=tick * 15), signals["cpu"], signals["queue"], signals["kv"], 1 if tick < 7 else 2, 1 if tick < 7 else 2, "local-synthetic", f"hpa-{tick}"))
    return result


@pytest.mark.parametrize("arm", [LocalArm.CPU, LocalArm.QUEUE, LocalArm.KV])
def test_each_arm_scales_independently(arm):
    result = evaluate_arm(arm, samples_for(arm))
    assert result.valid
    assert result.negative_control_seconds >= 90


def test_crosstalk_invalidates_proof():
    records = samples_for(LocalArm.CPU)
    records[-1] = records[-1].__class__(records[-1].timestamp, records[-1].cpu, 100, records[-1].kv, 2, 2, "local-synthetic", "hpa-10")
    assert not evaluate_arm(LocalArm.CPU, records).valid
