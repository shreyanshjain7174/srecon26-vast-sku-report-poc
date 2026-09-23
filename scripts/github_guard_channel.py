#!/usr/bin/env python3
"""GitHub issue-comment control channel for the independent deadline guard.

This program deliberately never receives a provider credential.  It translates
strict, owner-authored issue comments into nonce-bound guard-worker operations;
the worker alone reads the root-owned credential file and may destroy a target.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterable, Mapping

from guard.guard_worker import GuardSafetyError, GuardedInstance, GuardWorker, nonce_bound_label


NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
LABEL_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9-]{1,39}$")
IDENTITY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
ROOT_RE = re.compile(r"^[0-9a-f]{64}$")
COMMENT_RE = re.compile(r"^SRECON26_GUARD_V1 (HEARTBEAT|ANCHOR) nonce=([A-Za-z0-9_-]{8,128}) root=([0-9a-f]{64})$")
MAX_RUN = timedelta(minutes=60)
FUTURE_SKEW = timedelta(minutes=5)


class GuardChannelError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class GuardConfig:
    nonce: str
    label: str
    issue_number: int
    hard_deadline: datetime
    trusted_author: str
    heartbeat_timeout: timedelta
    armed_at: datetime


@dataclass(frozen=True, slots=True)
class CommentEvent:
    comment_id: int
    kind: str
    root_hash: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class GuardDecision:
    teardown_required: bool
    reason: str | None


class _AnchorOnlyProvider:
    """A provider sentinel: anchor-only mode must never reach a provider API."""

    calls = 0

    def find_instances(self, label: str) -> tuple[GuardedInstance, ...]:
        self.calls += 1
        raise GuardSafetyError("anchor-only mode cannot reconcile a provider")

    def get_instance(self, instance_id: int) -> GuardedInstance | None:
        self.calls += 1
        raise GuardSafetyError("anchor-only mode cannot query a provider")

    def destroy_exact(self, instance_id: int, expected_label: str) -> None:
        self.calls += 1
        raise GuardSafetyError("anchor-only mode cannot destroy a provider target")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise GuardChannelError("timestamps must be timezone-aware UTC")
    return value.astimezone(UTC)


def _stamp(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError as exc:
        raise GuardChannelError("timestamp must be ISO-8601 UTC") from exc


def parse_config(nonce: str, label: str, issue_number: str, deadline: str, trusted_author: str, heartbeat_seconds: str, *, now: datetime) -> GuardConfig:
    now = _utc(now)
    if not NONCE_RE.fullmatch(nonce):
        raise GuardChannelError("nonce must be 8-128 URL-safe characters")
    if not LABEL_RE.fullmatch(label):
        raise GuardChannelError("label must be 1-128 safe characters")
    if not LOGIN_RE.fullmatch(trusted_author):
        raise GuardChannelError("trusted author is not a GitHub login")
    try:
        issue = int(issue_number)
        heartbeat = timedelta(seconds=int(heartbeat_seconds))
    except ValueError as exc:
        raise GuardChannelError("issue number and heartbeat timeout must be integers") from exc
    if issue <= 0 or not timedelta(seconds=30) <= heartbeat <= timedelta(minutes=10):
        raise GuardChannelError("issue number or heartbeat timeout is out of bounds")
    hard_deadline = _parse_time(deadline)
    if hard_deadline <= now:
        raise GuardChannelError("hard deadline must be in the future")
    if hard_deadline - now > MAX_RUN:
        raise GuardChannelError("hard deadline must be within 60 minutes")
    return GuardConfig(nonce, label, issue, hard_deadline, trusted_author, heartbeat, now)


def accepted_comment_events(comments: Iterable[object], config: GuardConfig, *, now: datetime) -> tuple[list[CommentEvent], list[tuple[int, str]]]:
    """Accept only an exact, fresh OWNER comment from the dispatched owner."""
    now = _utc(now)
    accepted: list[CommentEvent] = []
    rejected: list[tuple[int, str]] = []
    for raw in comments:
        if not isinstance(raw, Mapping):
            continue
        try:
            comment_id = int(raw["id"])
            body = raw["body"]
            created_at = _parse_time(str(raw["created_at"]))
            user = raw["user"]
            author = user.get("login") if isinstance(user, Mapping) else None
            association = raw.get("author_association")
        except (KeyError, TypeError, ValueError, GuardChannelError):
            continue
        match = COMMENT_RE.fullmatch(body) if isinstance(body, str) else None
        if author != config.trusted_author or association != "OWNER":
            rejected.append((comment_id, "untrusted_author"))
        elif match is None or match.group(2) != config.nonce:
            rejected.append((comment_id, "invalid_or_wrong_nonce"))
        elif created_at < config.armed_at or created_at > now + FUTURE_SKEW:
            rejected.append((comment_id, "stale_or_future"))
        else:
            accepted.append(CommentEvent(comment_id, match.group(1), match.group(3), created_at))
    accepted.sort(key=lambda event: (event.created_at, event.comment_id))
    return accepted, rejected


def evaluate_guard(config: GuardConfig, *, last_heartbeat: datetime | None, now: datetime) -> GuardDecision:
    now = _utc(now)
    if now >= config.hard_deadline:
        return GuardDecision(True, "immutable_deadline")
    if last_heartbeat is None or now - _utc(last_heartbeat) > config.heartbeat_timeout:
        return GuardDecision(True, "heartbeat_loss")
    return GuardDecision(False, None)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _append_journal(path: Path, event: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = dict(event)
    payload["event_hash"] = hashlib.sha256(_canonical(payload)).hexdigest()
    with path.open("ab", buffering=0) as handle:
        handle.write(_canonical(payload) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())


def _write_state(path: Path, state: Mapping[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(_canonical(state) + b"\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _guard_receipt(receipt: object) -> dict[str, object]:
    raw = asdict(receipt)
    for key in ("hard_deadline", "last_heartbeat"):
        raw[key] = _stamp(raw[key])
    return raw


def anchor_only(
    *,
    nonce: str,
    label: str,
    run_id: str,
    host_identity: str,
    phase_roots: Mapping[str, str],
    root: Path,
    receipt_path: Path,
    now: datetime,
) -> dict[str, object]:
    """Record Phase 1 roots in a real guard journal without any provider path."""
    now = _utc(now)
    if not NONCE_RE.fullmatch(nonce) or not IDENTITY_RE.fullmatch(run_id) or not IDENTITY_RE.fullmatch(host_identity):
        raise GuardChannelError("nonce, run identity, or host identity is invalid")
    if host_identity in {"localhost", "127.0.0.1", "::1"}:
        raise GuardChannelError("host identity must not be local")
    if set(phase_roots) != {"cpu", "queue", "kv"} or any(not isinstance(value, str) or not ROOT_RE.fullmatch(value) for value in phase_roots.values()):
        raise GuardChannelError("Phase 1 roots must be exactly cpu, queue, and kv SHA-256 roots")
    expected_label = nonce_bound_label(f"phase1-anchor-{run_id}", nonce)
    if label != expected_label:
        raise GuardChannelError("anchor label must be derived from the run identity and nonce")
    provider = _AnchorOnlyProvider()
    worker = GuardWorker(root, provider, require_root_owner=False)
    worker.arm(None, label, nonce, now + timedelta(minutes=5), now=now)
    for _name, root_hash in sorted(phase_roots.items()):
        receipt = worker.anchor(nonce, root_hash, now=now)
    result = {
        "mode": "anchor_only",
        "host_identity": host_identity,
        "run_id": run_id,
        "label": label,
        "nonce": nonce,
        "phase1_roots": dict(phase_roots),
        "provider_calls": provider.calls,
        "guard_receipt": _guard_receipt(receipt),
    }
    _write_state(receipt_path, result)
    return result


def _config_record(config: GuardConfig) -> dict[str, object]:
    return {
        "nonce": config.nonce,
        "label": config.label,
        "issue_number": config.issue_number,
        "hard_deadline": _stamp(config.hard_deadline),
        "trusted_author": config.trusted_author,
        "heartbeat_seconds": int(config.heartbeat_timeout.total_seconds()),
        "armed_at": _stamp(config.armed_at),
    }


def _immutable_config_record(config: GuardConfig) -> dict[str, object]:
    record = _config_record(config)
    del record["armed_at"]
    return record


def _load_or_initialize_state(path: Path, config: GuardConfig) -> dict[str, object]:
    immutable = _config_record(config)
    if not path.exists():
        state = {"config": immutable, "last_comment_id": 0, "last_heartbeat": _stamp(config.armed_at)}
        _write_state(path, state)
        return state
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise GuardChannelError("channel state is corrupt") from exc
    if not isinstance(state, dict) or not isinstance(state.get("config"), Mapping) or {key: value for key, value in state["config"].items() if key != "armed_at"} != _immutable_config_record(config):
        raise GuardChannelError("channel configuration is immutable after arming")
    return state


def _worker(worker: Path, command: str, config: GuardConfig, root: Path, secret_file: Path, *, root_hash: str | None = None) -> dict[str, object]:
    command_line = [sys.executable, str(worker), command, "--root", str(root), "--secret-file", str(secret_file), "--nonce", config.nonce]
    if command == "arm":
        command_line.extend(["--label", config.label, "--hard-deadline", _stamp(config.hard_deadline)])
    if root_hash is not None:
        command_line.extend(["--root-hash", root_hash])
    completed = subprocess.run(command_line, check=True, capture_output=True, text=True)
    try:
        decoded = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise GuardChannelError("guard worker returned invalid receipt") from exc
    if not isinstance(decoded, dict):
        raise GuardChannelError("guard worker returned invalid receipt")
    return decoded


def arm(config: GuardConfig, *, worker: Path, root: Path, secret_file: Path, channel_state: Path, journal: Path) -> dict[str, object]:
    state = _load_or_initialize_state(channel_state, config)
    receipt = _worker(worker, "arm", config, root, secret_file)
    _append_journal(journal, {"event": "armed", "time": _stamp(config.armed_at), "nonce": config.nonce, "receipt": receipt})
    return state


def sync(config: GuardConfig, comments: Iterable[object], *, worker: Path, root: Path, secret_file: Path, channel_state: Path, journal: Path, now: datetime) -> dict[str, object]:
    now = _utc(now)
    state = _load_or_initialize_state(channel_state, config)
    persisted = state["config"]
    if not isinstance(persisted, Mapping):
        raise GuardChannelError("channel configuration is corrupt")
    config = GuardConfig(
        config.nonce,
        config.label,
        config.issue_number,
        config.hard_deadline,
        config.trusted_author,
        config.heartbeat_timeout,
        _parse_time(str(persisted["armed_at"])),
    )
    last_id = int(state.get("last_comment_id", 0))
    events, rejected = accepted_comment_events(comments, config, now=now)
    for comment_id, reason in rejected:
        _append_journal(journal, {"event": "comment_rejected", "time": _stamp(now), "nonce": config.nonce, "comment_id": comment_id, "reason": reason})
    for event in events:
        if event.comment_id <= last_id:
            continue
        receipt = _worker(worker, "heartbeat" if event.kind == "HEARTBEAT" else "anchor", config, root, secret_file, root_hash=event.root_hash if event.kind == "ANCHOR" else None)
        if event.kind == "HEARTBEAT":
            state["last_heartbeat"] = _stamp(event.created_at)
        last_id = event.comment_id
        _append_journal(journal, {"event": event.kind.lower(), "time": _stamp(now), "nonce": config.nonce, "comment_id": event.comment_id, "root_hash": event.root_hash, "receipt": receipt})
    state["last_comment_id"] = last_id
    state["last_sync"] = _stamp(now)
    _write_state(channel_state, state)
    receipt = _worker(worker, "tick", config, root, secret_file)
    decision = evaluate_guard(config, last_heartbeat=_parse_time(str(state["last_heartbeat"])), now=now)
    _append_journal(journal, {"event": "tick", "time": _stamp(now), "nonce": config.nonce, "decision": asdict(decision), "receipt": receipt})
    return {"decision": asdict(decision), "receipt": receipt}


def main() -> int:
    parser = argparse.ArgumentParser(description="Nonce-bound GitHub issue channel for the deadline guard")
    parser.add_argument("command", choices=("validate", "arm", "sync", "anchor-only"))
    parser.add_argument("--nonce")
    parser.add_argument("--label")
    parser.add_argument("--issue-number")
    parser.add_argument("--hard-deadline")
    parser.add_argument("--trusted-author")
    parser.add_argument("--heartbeat-seconds", default="120")
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--secret-file", type=Path)
    parser.add_argument("--channel-state", type=Path)
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--comments-json", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--host-identity")
    parser.add_argument("--cpu-root")
    parser.add_argument("--queue-root")
    parser.add_argument("--kv-root")
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    if args.command == "anchor-only":
        required = (args.nonce, args.label, args.run_id, args.host_identity, args.cpu_root, args.queue_root, args.kv_root, args.root, args.receipt)
        if any(item is None for item in required):
            parser.error("anchor-only requires nonce, label, run/host identity, three roots, root, and receipt")
        result = anchor_only(
            nonce=args.nonce,
            label=args.label,
            run_id=args.run_id,
            host_identity=args.host_identity,
            phase_roots={"cpu": args.cpu_root, "queue": args.queue_root, "kv": args.kv_root},
            root=args.root,
            receipt_path=args.receipt,
            now=datetime.now(UTC),
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    if not all((args.nonce, args.label, args.issue_number, args.hard_deadline, args.trusted_author)):
        parser.error("validate, arm, and sync require nonce, label, issue number, deadline, and trusted author")
    config = parse_config(args.nonce, args.label, args.issue_number, args.hard_deadline, args.trusted_author, args.heartbeat_seconds, now=datetime.now(UTC))
    if args.command == "validate":
        print(json.dumps(_config_record(config), sort_keys=True))
        return 0
    required = (args.worker, args.root, args.secret_file, args.channel_state, args.journal)
    if any(item is None for item in required):
        parser.error("arm and sync require worker, root, secret file, channel state, and journal paths")
    if args.command == "arm":
        arm(config, worker=args.worker, root=args.root, secret_file=args.secret_file, channel_state=args.channel_state, journal=args.journal)
        return 0
    if args.comments_json is None:
        parser.error("sync requires --comments-json")
    raw_comments = json.loads(args.comments_json.read_text(encoding="utf-8"))
    if not isinstance(raw_comments, list):
        raise GuardChannelError("comments response must be a list")
    print(json.dumps(sync(config, raw_comments, worker=args.worker, root=args.root, secret_file=args.secret_file, channel_state=args.channel_state, journal=args.journal, now=datetime.now(UTC)), sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (GuardChannelError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        print(f"github guard channel refused request: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(2)
