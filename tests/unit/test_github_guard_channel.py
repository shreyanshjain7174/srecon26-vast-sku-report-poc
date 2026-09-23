from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.github_guard_channel import (
    GuardChannelError,
    GuardConfig,
    accepted_comment_events,
    evaluate_guard,
    parse_config,
)


NOW = datetime(2026, 9, 23, 4, 0, tzinfo=UTC)
NONCE = "nonce_12345678"
ROOT = "a" * 64


def config(**overrides: object) -> GuardConfig:
    values: dict[str, object] = {
        "nonce": NONCE,
        "label": "srecon26-poc-nonce_12345678",
        "issue_number": 17,
        "hard_deadline": NOW + timedelta(minutes=30),
        "trusted_author": "sunny",
        "heartbeat_timeout": timedelta(minutes=2),
        "armed_at": NOW,
    }
    values.update(overrides)
    return GuardConfig(**values)  # type: ignore[arg-type]


def comment(body: str, **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": 42,
        "body": body,
        "created_at": "2026-09-23T04:00:01Z",
        "user": {"login": "sunny"},
        "author_association": "OWNER",
    }
    value.update(overrides)
    return value


def test_input_validation_rejects_unbounded_or_unsafe_dispatch_values() -> None:
    with pytest.raises(GuardChannelError, match="nonce"):
        parse_config("bad nonce", "label", "17", "2026-09-23T04:30:00Z", "sunny", "120", now=NOW)
    with pytest.raises(GuardChannelError, match="future"):
        parse_config(NONCE, "label", "17", "2026-09-23T04:00:00Z", "sunny", "120", now=NOW)
    with pytest.raises(GuardChannelError, match="60 minutes"):
        parse_config(NONCE, "label", "17", "2026-09-23T05:01:00Z", "sunny", "120", now=NOW)


def test_spoofed_or_stale_heartbeats_are_rejected() -> None:
    valid = f"SRECON26_GUARD_V1 HEARTBEAT nonce={NONCE} root={ROOT}"
    comments = [
        comment(valid, created_at="2026-09-23T03:59:59Z"),
        comment(valid, id=43, user={"login": "attacker"}),
        comment(valid.replace(NONCE, "different_12345678"), id=44),
        comment(valid, id=45),
    ]
    events, rejected = accepted_comment_events(comments, config(), now=NOW + timedelta(seconds=10))
    assert [event.kind for event in events] == ["HEARTBEAT"]
    assert {item[0] for item in rejected} == {42, 43, 44}


def test_deadline_is_immutable_even_with_a_fresh_heartbeat() -> None:
    decision = evaluate_guard(config(), last_heartbeat=NOW + timedelta(minutes=29), now=NOW + timedelta(minutes=30))
    assert decision.teardown_required
    assert decision.reason == "immutable_deadline"


def test_workflow_has_restricted_permissions_bounded_job_and_secret_only_credential_path() -> None:
    workflow = Path(".github/workflows/independent-guard.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert "Verify private issue control channel" in workflow
    assert "timeout-minutes: 60" in workflow
    assert "contents: read" in workflow
    assert "issues: write" in workflow
    assert "secrets.VAST_API_KEY" in workflow
    assert workflow.count("secrets.VAST_API_KEY") == 1
    assert "vars.VAST_API_KEY" not in workflow
    assert "pull_request:" not in workflow
    assert "schedule:" not in workflow
    assert "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683" in workflow
    assert "actions/upload-artifact@65c4c4a1ddee5b72f698fdd19549f0f0fb45cf08" in workflow
