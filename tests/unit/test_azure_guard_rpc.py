from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import textwrap
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from guard import ssh_rpc
from guard.guard_worker import GuardSafetyError, GuardWorker, GuardedInstance
from srecon26_poc.azure_guard_transport import _validate_response


UTC = timezone.utc


NOW = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
NONCE = "nonce_12345678"
LABEL = f"srecon26-run-1--nonce-{NONCE}"
METADATA = {
    "host_identity": "independent-azure-vm",
    "azure_resource_id": "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/guard-rg/providers/Microsoft.Compute/virtualMachines/guard-vm",
    "azure_vm_id": "22222222-2222-2222-2222-222222222222",
    "host_key_fingerprint": "SHA256:V/wGqHTHSNb4BFrleEaT0jG2C+WQ+j9+BcxG9WeR+6I",
}


class Provider:
    def __init__(self) -> None:
        self.instances: dict[int, GuardedInstance] = {}
        self.destroyed: list[int] = []
        self.inventory_reads = 0

    def instance_inventory_count(self) -> int:
        self.inventory_reads += 1
        return len(self.instances)

    def find_instances(self, label: str) -> tuple[GuardedInstance, ...]:
        return tuple(item for item in self.instances.values() if item.label == label)

    def get_instance(self, instance_id: int) -> GuardedInstance | None:
        return self.instances.get(instance_id)

    def destroy_exact(self, instance_id: int, expected_label: str) -> None:
        assert self.instances[instance_id].label == expected_label
        self.destroyed.append(instance_id)
        del self.instances[instance_id]


def arm_payload(**updates: object) -> dict[str, object]:
    return {
        "run_id": "run-1", "nonce": NONCE, "label": LABEL,
        "hard_deadline": "2026-09-24T10:05:00Z", "heartbeat_timeout_seconds": 45,
        **updates,
    }


def request(command: str, payload: dict[str, object]) -> bytes:
    return (json.dumps({"protocol": ssh_rpc.PROTOCOL, "command": command, "payload": payload}) + "\n").encode()


def dispatch_at(server, command, payload, *, now):
    server.clock = lambda: now
    return server.dispatch(command, payload)


def tick_at(server, *, now):
    server.clock = lambda: now
    return server.tick_all()


@pytest.fixture
def gateway(tmp_path: Path):
    previous = os.umask(0o077)
    provider = Provider()
    worker = GuardWorker(tmp_path / "state", provider, require_root_owner=False)
    gateway = ssh_rpc.GuardGateway(worker, METADATA, require_root_owner=False)
    try:
        yield gateway, provider
    finally:
        os.umask(previous)


@pytest.mark.parametrize("command", [None, "", "guardctl ", " guardctl", "guardctl; id", "guardctl\n", "bash", "guardctl tick-all", "guardctl install-credential"])
def test_runtime_rejects_every_nonliteral_command_before_reading(monkeypatch, command):
    monkeypatch.setattr(ssh_rpc.os, "geteuid", lambda: 0)
    class Unreadable:
        def read(self, _limit):
            pytest.fail("rejected command must not consume input")
    code, raw = ssh_rpc.serve([], command, Unreadable())
    assert code == 2
    assert json.loads(raw) == {"status": "REFUSED", "error": "guard request refused"}
    assert raw.count(b"\n") == 1


@pytest.mark.parametrize(("arguments", "original"), [
    (["tick-all"], "guardctl tick-all"),
    (["install-credential"], "guardctl"),
    (["install-credential"], None),
    (["install-credential"], "guardctl install-credential "),
    (["install-credential", "extra"], "guardctl install-credential"),
    (["status"], "guardctl"),
])
def test_entrypoints_cannot_cross_runtime_bootstrap_or_timer_boundary(monkeypatch, arguments, original):
    monkeypatch.setattr(ssh_rpc.os, "geteuid", lambda: 0)
    code, raw = ssh_rpc.serve(arguments, original, io.BytesIO(b"secret-sentinel\n"))
    assert code == 2 and b"secret-sentinel" not in raw


def test_non_root_cannot_enter_gateway_or_install_secret(monkeypatch, tmp_path):
    monkeypatch.setattr(ssh_rpc.os, "geteuid", lambda: 1234)
    secret = tmp_path / "secret"
    for args, original in [([], "guardctl"), (["install-credential"], "guardctl install-credential"), (["tick-all"], None)]:
        assert ssh_rpc.serve(args, original, io.BytesIO(b"never-output\n"), credential_path=secret)[0] == 2
    assert not secret.exists()


@pytest.mark.parametrize("raw", [
    b"[]", b"null", b"{}", b"{bad}", b"\xff", b"{}\n{}", b"[" * 2000,
    request("tick-all", {}), request("install-credential", {}),
    request("status", {"nonce": "../../secret"}),
    request("preflight", {"credential": "never-output"}),
    request("arm", arm_payload(heartbeat_timeout_seconds=True)),
    request("arm", arm_payload(heartbeat_timeout_seconds=0)),
    request("arm", arm_payload(heartbeat_timeout_seconds=601)),
    request("arm", arm_payload(hard_deadline="2026-09-24T10:05:00")),
    request("arm", arm_payload(hard_deadline="2026-09-24T10:05:00+05:30")),
    request("arm", arm_payload(label="prefix--nonce-other--nonce-" + NONCE)),
    request("arm", arm_payload(run_id="run;id")),
    request("heartbeat", {"nonce": NONCE, "label": LABEL, "run_id": "run-1", "monotonic_ns": -1}),
    request("heartbeat", {"nonce": NONCE, "label": LABEL, "run_id": "run-1", "monotonic_ns": True}),
    request("heartbeat", {"nonce": NONCE, "label": LABEL, "run_id": "run-1", "monotonic_ns": 2**64}),
    request("export", {"nonce": NONCE, "root_hash": "A" * 64}),
    b'{"protocol":"srecon26-guard-v1","command":"preflight","command":"preflight","payload":{}}',
    b'{"protocol":"srecon26-guard-v1","command":"status","payload":{"nonce":NaN}}',
    request("preflight", {}).replace(b"srecon26-guard-v1", b"unknown-v2"),
    b" " * (ssh_rpc.MAX_REQUEST_BYTES + 1),
])
def test_invalid_requests_return_one_fixed_response(monkeypatch, raw):
    monkeypatch.setattr(ssh_rpc.os, "geteuid", lambda: 0)
    def never_called():
        pytest.fail("invalid request reached production factory")
    code, result = ssh_rpc.serve([], "guardctl", io.BytesIO(raw), gateway_factory=never_called)
    assert code == 2
    assert json.loads(result)["status"] == "REFUSED"
    assert result.count(b"\n") == 1 and len(result) < 100


def test_input_reads_are_bounded():
    class BoundedStream:
        def read(self, count):
            assert count == ssh_rpc.MAX_REQUEST_BYTES + 1
            return b"x" * count
    with pytest.raises(GuardSafetyError):
        ssh_rpc.read_bounded(BoundedStream(), ssh_rpc.MAX_REQUEST_BYTES)


def test_all_successful_runtime_responses_satisfy_real_client(gateway):
    server, provider = gateway
    payload = arm_payload()
    arm = dispatch_at(server, "arm", payload, now=NOW)
    assert provider.inventory_reads == 1
    binding = json.loads(ssh_rpc._encode(arm))
    _validate_response("arm", payload, ssh_rpc._encode(arm).decode(), None)
    for command, body in [
        ("preflight", {}),
        ("heartbeat", {"run_id": "run-1", "label": LABEL, "nonce": NONCE, "monotonic_ns": 3}),
        ("status", {"nonce": NONCE}),
        ("anchor", {"nonce": NONCE, "root_hash": "a" * 64}),
    ]:
        receipt = dispatch_at(server, command, body, now=NOW + timedelta(seconds=5))
        _validate_response(command, body, ssh_rpc._encode(receipt).decode(), binding)
        assert receipt["heartbeat_timeout_seconds"] == 45
    body = {"nonce": NONCE, "root_hash": receipt["root_hash"]}
    exported = dispatch_at(server, "export", body, now=NOW)
    _validate_response("export", body, ssh_rpc._encode(exported).decode(), binding)
    assert hashlib.sha256(exported["journal"].encode()).hexdigest() == exported["journal_sha256"]
    assert json.loads(exported["journal"].splitlines()[-1])["event_hash"] == exported["root_hash"]


def test_preflight_attests_waiting_worker_without_changing_durable_state(gateway):
    server, _ = gateway
    dispatch_at(server, "arm", arm_payload(), now=NOW)
    tick_at(server, now=NOW + timedelta(seconds=1))
    response = dispatch_at(server, "preflight", {}, now=NOW + timedelta(seconds=2))
    assert response["status"] == "ARMED" and response["worker_status"] == "AWAITING_INSTANCE"
    assert server.worker.status(NONCE).status == "AWAITING_INSTANCE"


def test_arm_replay_preserves_timeout_heartbeat_and_reconciled_instance(gateway):
    server, provider = gateway
    first = dispatch_at(server, "arm", arm_payload(), now=NOW)
    provider.instances[417] = GuardedInstance(417, LABEL)
    tick_at(server, now=NOW + timedelta(seconds=1))
    repeated = dispatch_at(server, "arm", arm_payload(), now=NOW + timedelta(seconds=10))
    assert repeated["last_heartbeat"] == first["last_heartbeat"]
    assert repeated["instance_id"] == 417
    assert provider.inventory_reads == 1
    for update in ({"heartbeat_timeout_seconds": 120}, {"run_id": "different"}, {"hard_deadline": "2026-09-24T11:00:00Z"}):
        with pytest.raises(GuardSafetyError):
            dispatch_at(server, "arm", arm_payload(**update), now=NOW + timedelta(seconds=10))


def test_restarted_timer_uses_persisted_timeout_and_retries_absence(gateway):
    server, provider = gateway
    dispatch_at(server, "arm", arm_payload(), now=NOW)
    provider.instances[417] = GuardedInstance(417, LABEL)
    assert tick_at(server, now=NOW + timedelta(seconds=1))
    restarted = ssh_rpc.GuardGateway(GuardWorker(server.root, provider, heartbeat_timeout=timedelta(seconds=600), require_root_owner=False), METADATA, require_root_owner=False)
    assert tick_at(restarted, now=NOW + timedelta(seconds=46))
    assert provider.destroyed == [417]
    for seconds in (47, 48, 49):
        assert tick_at(restarted, now=NOW + timedelta(seconds=seconds))
    receipt = dispatch_at(restarted, "status", {"nonce": NONCE}, now=NOW)
    assert receipt["status"] == "ABSENCE_CONFIRMED"
    assert len(receipt["absence_observations"]) == 3


def test_expired_heartbeat_cannot_be_revived_before_timer_runs(gateway):
    server, _ = gateway
    dispatch_at(server, "arm", arm_payload(), now=NOW)
    for command, payload in [
        ("arm", arm_payload()), ("preflight", {}),
        ("heartbeat", {"run_id": "run-1", "label": LABEL, "nonce": NONCE, "monotonic_ns": 1}),
    ]:
        with pytest.raises(GuardSafetyError):
            dispatch_at(server, command, payload, now=NOW + timedelta(seconds=46))


def test_arm_checks_empty_account_and_refuses_other_unresolved_run(gateway):
    server, provider = gateway
    provider.instances[88] = GuardedInstance(88, "unrelated")
    with pytest.raises(GuardSafetyError):
        dispatch_at(server, "arm", arm_payload(), now=NOW)
    assert not (server.root / NONCE).exists()
    provider.instances.clear()
    dispatch_at(server, "arm", arm_payload(), now=NOW)
    other = arm_payload(nonce="nonce_other123", label="other--nonce-nonce_other123")
    with pytest.raises(GuardSafetyError):
        dispatch_at(server, "arm", other, now=NOW)
    for seconds in (301, 302, 303, 304):
        tick_at(server, now=NOW + timedelta(seconds=seconds))
    assert server.worker.status(NONCE).status == "ABSENCE_CONFIRMED"
    other["hard_deadline"] = "2026-09-24T10:15:00Z"
    assert dispatch_at(server, "arm", other, now=NOW + timedelta(seconds=305))["status"] == "ARMED"


def test_unknown_status_does_not_create_durable_run(gateway):
    server, _ = gateway
    dispatch_at(server, "arm", arm_payload(), now=NOW)
    with pytest.raises(FileNotFoundError):
        dispatch_at(server, "status", {"nonce": "unknown_nonce"}, now=NOW)
    assert not (server.root / "unknown_nonce").exists()


def test_export_rejects_stale_root_and_tampering(gateway):
    server, _ = gateway
    receipt = dispatch_at(server, "arm", arm_payload(), now=NOW)
    dispatch_at(server, "anchor", {"nonce": NONCE, "root_hash": "b" * 64}, now=NOW)
    with pytest.raises(GuardSafetyError):
        dispatch_at(server, "export", {"nonce": NONCE, "root_hash": receipt["root_hash"]}, now=NOW)
    journal = server.root / NONCE / "journal.ndjson"
    current = server.worker.status(NONCE)
    journal.write_bytes(journal.read_bytes().replace(b'"armed"', b'"altered"'))
    with pytest.raises(GuardSafetyError):
        dispatch_at(server, "export", {"nonce": NONCE, "root_hash": current.root_hash}, now=NOW)


def test_export_response_limit_counts_json_escaping(gateway, monkeypatch):
    server, _ = gateway
    receipt = dispatch_at(server, "arm", arm_payload(), now=NOW)
    journal_bytes = (server.root / NONCE / "journal.ndjson").stat().st_size
    monkeypatch.setattr(ssh_rpc, "MAX_RESPONSE_BYTES", journal_bytes + 20)
    with pytest.raises(GuardSafetyError):
        dispatch_at(server, "export", {"nonce": NONCE, "root_hash": receipt["root_hash"]}, now=NOW)


def test_broken_prior_run_does_not_stop_other_timer_tick(gateway):
    server, provider = gateway
    dispatch_at(server, "arm", arm_payload(), now=NOW)
    provider.instances[417] = GuardedInstance(417, LABEL)
    tick_at(server, now=NOW + timedelta(seconds=1))
    (server.root / "aaa_crashed_arm").mkdir(mode=0o700)
    assert not tick_at(server, now=NOW + timedelta(seconds=46))
    assert provider.destroyed == [417]


def test_credential_is_exclusive_private_and_not_a_runtime_schema(tmp_path):
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    path = parent / "secret"
    ssh_rpc.install_credential(io.BytesIO(b"credential-sentinel\n"), path, require_root_owner=False)
    assert path.read_bytes() == b"credential-sentinel\n"
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        ssh_rpc.install_credential(io.BytesIO(b"replacement\n"), path, require_root_owner=False)
    assert path.read_bytes() == b"credential-sentinel\n"
    with pytest.raises(GuardSafetyError):
        ssh_rpc.validate_request(request("install-credential", {"credential": "credential-sentinel"}))


@pytest.mark.parametrize("content", [b"", b"\n", b"a b", b"a\nb", b"a\r\n", b"x" * 4097])
def test_bootstrap_rejects_empty_multiline_or_oversize_secret(tmp_path, content):
    tmp_path.chmod(0o700)
    path = tmp_path / "secret"
    with pytest.raises(GuardSafetyError):
        ssh_rpc.install_credential(io.BytesIO(content), path, require_root_owner=False)
    assert not path.exists()


def test_secret_permissions_owner_and_symlink_fail_closed(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    path = tmp_path / "secret"
    path.write_text("sentinel")
    path.chmod(0o644)
    with pytest.raises(GuardSafetyError):
        ssh_rpc.read_private(path, 100, require_root_owner=False)
    path.chmod(0o600)
    real_lstat = Path.lstat
    def non_root_lstat(self):
        info = real_lstat(self)
        values = list(info)
        values[4] = 1234
        return os.stat_result(values)
    monkeypatch.setattr(Path, "lstat", non_root_lstat)
    with pytest.raises(GuardSafetyError):
        ssh_rpc.read_private(path, 100)
    monkeypatch.undo()
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(GuardSafetyError):
        ssh_rpc.read_private(link, 100, require_root_owner=False)
    with pytest.raises(FileExistsError):
        ssh_rpc.install_credential(io.BytesIO(b"replacement"), link, require_root_owner=False)
    assert path.read_text() == "sentinel"


def test_private_directory_is_not_silently_repaired(tmp_path):
    tmp_path.chmod(0o755)
    with pytest.raises(GuardSafetyError):
        ssh_rpc.install_credential(io.BytesIO(b"sentinel"), tmp_path / "secret", require_root_owner=False)


def test_provider_exception_cannot_escape_in_response(monkeypatch):
    monkeypatch.setattr(ssh_rpc.os, "geteuid", lambda: 0)
    def failing_factory():
        raise RuntimeError("provider credential-sentinel stderr secret")
    code, raw = ssh_rpc.serve([], "guardctl", io.BytesIO(request("preflight", {})), gateway_factory=failing_factory)
    assert code == 2 and b"credential-sentinel" not in raw and b"stderr" not in raw


def test_main_suppresses_accidental_diagnostics_and_emits_only_response(monkeypatch, capfd):
    expected = b'{"status":"REFUSED"}\n'
    def noisy_serve(*_args):
        print("private-diagnostic-sentinel")
        print("private-diagnostic-sentinel", file=ssh_rpc.sys.stderr)
        return 2, expected
    monkeypatch.setattr(ssh_rpc, "serve", noisy_serve)
    monkeypatch.setattr(ssh_rpc.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(ssh_rpc.signal, "alarm", lambda *_args: None)
    monkeypatch.setattr(ssh_rpc.sys, "argv", ["ssh_rpc.py"])
    previous = os.umask(0o077)
    try:
        assert ssh_rpc.main() == 2
    finally:
        os.umask(previous)
    captured = capfd.readouterr()
    assert captured.out.encode() == expected and captured.err == ""


def test_local_timer_entrypoint_never_reads_stdin(monkeypatch):
    monkeypatch.setattr(ssh_rpc.os, "geteuid", lambda: 0)
    class Timer:
        def tick_all(self):
            return True
    code, raw = ssh_rpc.serve(["tick-all"], None, io.BytesIO(), gateway_factory=Timer)
    assert code == 0 and json.loads(raw)["status"] == "TICK_COMPLETE"


@pytest.mark.parametrize("active,enabled", [("active", "enabled"), ("inactive", "enabled"), ("active", "disabled")])
def test_arm_and_preflight_require_live_enabled_timer(monkeypatch, gateway, active, enabled):
    server, provider = gateway
    def systemctl(arguments, **kwargs):
        assert arguments == ["/usr/bin/systemctl", "show", "--property=LoadState,ActiveState,UnitFileState", "srecon26-azure-guard.timer"]
        assert kwargs["timeout"] == 3 and kwargs["env"] == {"PATH": "/usr/bin:/bin"}
        return subprocess.CompletedProcess(arguments, 0, f"LoadState=loaded\nActiveState={active}\nUnitFileState={enabled}\n", "")
    monkeypatch.setattr(ssh_rpc.subprocess, "run", systemctl)
    server.readiness_check = ssh_rpc.check_timer_ready
    if active == "active" and enabled == "enabled":
        assert dispatch_at(server, "arm", arm_payload(), now=NOW)["status"] == "ARMED"
        assert dispatch_at(server, "preflight", {}, now=NOW)["status"] == "ARMED"
    else:
        with pytest.raises(GuardSafetyError):
            dispatch_at(server, "arm", arm_payload(), now=NOW)
        assert provider.inventory_reads == 0


def test_installer_and_templates_use_ubuntu_paths_and_private_minimal_environment():
    root = Path(__file__).resolve().parents[2]
    installer = root / "guard/install_azure_guard.sh"
    subprocess.run(["bash", "-n", str(installer)], check=True, capture_output=True)
    subprocess.run(["sh", "-n", str(root / "guard/guardctl.template")], check=True, capture_output=True)
    content = installer.read_text()
    assert "stat -f" not in content and "stat -c" in content
    assert "sys.version_info < (3, 10)" in content
    assert "safe_path" in content and "[[ ! -L ${candidate} ]]" in content
    assert "600" in content and "700" in content
    timer = (root / "guard/srecon26-azure-guard.timer.template").read_text()
    assert "OnCalendar=*-*-* *:*:00,15,30,45" in timer and "Persistent=true" in timer
    service = (root / "guard/srecon26-azure-guard.service.template").read_text()
    assert "User=root" in service and "UMask=0077" in service and "guardctl tick-all" in service
    assert "VAST_API_KEY" not in service and "StandardOutput=null" in service
    launcher = (root / "guard/guardctl.template").read_text()
    assert "/usr/bin/env -i" in launcher and "/usr/bin/python3 -I" in launcher


@pytest.mark.parametrize("updates", [
    {"extra": "field"}, {"vast_bin": "/home/user/vastai"},
    {"vast_bin": "/opt/vast/../vastai"}, {"vast_bin": "/opt/vast cli/vastai"},
    {"azure_vm_id": "not-a-uuid"}, {"azure_resource_id": "wrong-resource"},
])
def test_installer_metadata_schema_is_strict(updates):
    config = {key: METADATA[key] for key in ("azure_resource_id", "azure_vm_id")}
    config["vast_bin"] = "/opt/vast/bin/vastai"
    with pytest.raises((GuardSafetyError, ValueError)):
        ssh_rpc.validate_config({**config, **updates})


@pytest.mark.parametrize("block_at", ["gateway_lock", "readiness", "provider"])
def test_arm_rechecks_current_clock_after_blocking_setup(gateway, monkeypatch, block_at):
    server, provider = gateway
    current = [NOW]
    server.clock = lambda: current[0]
    def expire():
        current[0] = NOW + timedelta(minutes=6)
    if block_at == "gateway_lock":
        original = server.locked
        @contextmanager
        def delayed_lock():
            with original():
                expire()
                yield
        monkeypatch.setattr(server, "locked", delayed_lock)
    elif block_at == "readiness":
        server.readiness_check = expire
    else:
        def delayed_inventory():
            expire()
            return 0
        monkeypatch.setattr(provider, "instance_inventory_count", delayed_inventory)
    with pytest.raises(GuardSafetyError):
        server.dispatch("arm", arm_payload(), now=NOW)
    assert not (server.root / NONCE).exists()


@pytest.mark.parametrize("command", ["arm", "preflight", "heartbeat"])
@pytest.mark.parametrize("expired_by", ["heartbeat", "deadline"])
def test_authorization_rechecks_after_receipt_read_despite_stale_request_time(gateway, monkeypatch, command, expired_by):
    server, _ = gateway
    dispatch_at(server, "arm", arm_payload(), now=NOW)
    current = [NOW + timedelta(seconds=1)]
    server.clock = lambda: current[0]
    original = server._bound_receipt
    def delayed_receipt(binding):
        receipt = original(binding)
        current[0] = NOW + timedelta(seconds=46 if expired_by == "heartbeat" else 301)
        return receipt
    monkeypatch.setattr(server, "_bound_receipt", delayed_receipt)
    payload = arm_payload() if command == "arm" else {}
    if command == "heartbeat":
        payload = {"run_id": "run-1", "label": LABEL, "nonce": NONCE, "monotonic_ns": 1}
    with pytest.raises(GuardSafetyError):
        server.dispatch(command, payload, now=NOW)
    assert server.worker.status(NONCE).last_heartbeat == NOW


def test_heartbeat_rechecks_current_time_after_worker_lock_and_state_read(gateway, monkeypatch):
    server, _ = gateway
    dispatch_at(server, "arm", arm_payload(), now=NOW)
    current = [NOW + timedelta(seconds=1)]
    server.clock = lambda: current[0]
    original = server.worker._load
    calls = 0
    def delayed_worker_load(directory):
        nonlocal calls
        state = original(directory)
        calls += 1
        # First read is the gateway receipt; second is under heartbeat's lock.
        if calls == 2:
            current[0] = NOW + timedelta(seconds=46)
        return state
    monkeypatch.setattr(server.worker, "_load", delayed_worker_load)
    payload = {"run_id": "run-1", "label": LABEL, "nonce": NONCE, "monotonic_ns": 1}
    with pytest.raises(GuardSafetyError, match="authorization has expired"):
        server.dispatch("heartbeat", payload, now=NOW)
    assert server.worker.status(NONCE).last_heartbeat == NOW
    assert server.worker.status(NONCE).event_count == 1


@pytest.mark.parametrize("command", ["arm", "heartbeat"])
def test_no_armed_response_if_durable_write_finishes_after_deadline(gateway, monkeypatch, command):
    server, _ = gateway
    current = [NOW]
    if command == "heartbeat":
        dispatch_at(server, "arm", arm_payload(), now=NOW)
        current[0] += timedelta(seconds=1)
    server.clock = lambda: current[0]
    original = server.worker._durable_replace
    def delayed_persist(path, payload):
        original(path, payload)
        if path.name == ("rpc-binding.json" if command == "arm" else "state.json"):
            current[0] = NOW + timedelta(minutes=6)
    monkeypatch.setattr(server.worker, "_durable_replace", delayed_persist)
    payload = arm_payload() if command == "arm" else {"run_id": "run-1", "label": LABEL, "nonce": NONCE, "monotonic_ns": 1}
    with pytest.raises(GuardSafetyError):
        server.dispatch(command, payload, now=NOW)
    # Persistence keeps the watcher available, but never claims it is still safe.
    assert (server.root / NONCE / "state.json").is_file()


def test_process_deadline_bypasses_provider_worker_and_tick_recovery(gateway, monkeypatch):
    server, provider = gateway
    dispatch_at(server, "arm", arm_payload(), now=NOW)
    provider.instances[417] = GuardedInstance(417, LABEL)
    tick_at(server, now=NOW + timedelta(seconds=1))
    def expire_during_destroy(*_args):
        ssh_rpc._deadline(ssh_rpc.signal.SIGALRM, None)
    monkeypatch.setattr(provider, "destroy_exact", expire_during_destroy)
    with pytest.raises(ssh_rpc.GuardProcessDeadline):
        tick_at(server, now=NOW + timedelta(seconds=46))
    assert server.worker.status(NONCE).status == "TEARDOWN_REQUESTED"


def test_serve_does_not_handle_process_deadline(monkeypatch):
    monkeypatch.setattr(ssh_rpc.os, "geteuid", lambda: 0)
    class ExpiringTimer:
        def tick_all(self):
            ssh_rpc._deadline(ssh_rpc.signal.SIGALRM, None)
    with pytest.raises(ssh_rpc.GuardProcessDeadline):
        ssh_rpc.serve(["tick-all"], None, io.BytesIO(), gateway_factory=ExpiringTimer)


def test_direct_timer_process_deadline_cannot_be_swallowed_by_worker(tmp_path):
    source = textwrap.dedent('''
        import functools, os, signal, sys, time
        from datetime import datetime, timedelta, timezone
        from pathlib import Path
        from guard import ssh_rpc
        from guard.guard_worker import GuardWorker, GuardedInstance
        UTC = timezone.utc
        os.umask(0o077)
        class Provider:
            def instance_inventory_count(self): return 0
            def find_instances(self, label): return (GuardedInstance(417, label),)
            def get_instance(self, instance_id): return GuardedInstance(instance_id, label)
            def destroy_exact(self, *args): time.sleep(10)
        nonce = "direct_deadline_123"
        label = "direct--nonce-" + nonce
        start = datetime.now(UTC)
        worker = GuardWorker(Path(sys.argv[1]) / "state", Provider(), heartbeat_timeout=timedelta(seconds=1), require_root_owner=False)
        worker.arm(417, label, nonce, start + timedelta(minutes=5), now=start)
        gateway = ssh_rpc.GuardGateway(worker, {}, require_root_owner=False, clock=lambda: start + timedelta(seconds=2))
        ssh_rpc.os.geteuid = lambda: 0
        ssh_rpc.serve = functools.partial(ssh_rpc.serve, gateway_factory=lambda: gateway)
        actual_alarm = signal.alarm
        signal.alarm = lambda duration: actual_alarm(1 if duration else 0)
        sys.argv = ["guardctl", "tick-all"]
        raise SystemExit(ssh_rpc.main())
    ''')
    started = time.monotonic()
    completed = subprocess.run(
        [sys.executable, "-c", source, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[2], stdin=subprocess.DEVNULL,
        capture_output=True, timeout=4,
        env={"PATH": os.defpath, "PYTHONPATH": ".", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert time.monotonic() - started < 3
    assert completed.returncode == 2 and completed.stderr == b""
    assert json.loads(completed.stdout) == {"status": "REFUSED", "error": "guard request refused"}


def test_process_boundary_is_quiet_when_output_disconnects(monkeypatch, capfd):
    monkeypatch.setattr(ssh_rpc, "serve", lambda *_args: (0, b'{"status":"TICK_COMPLETE"}\n'))
    monkeypatch.setattr(ssh_rpc.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(ssh_rpc.signal, "alarm", lambda *_args: None)
    def disconnected(*_args):
        raise BrokenPipeError()
    monkeypatch.setattr(ssh_rpc.os, "write", disconnected)
    previous = os.umask(0o077)
    try:
        assert ssh_rpc.main() == 2
    finally:
        os.umask(previous)
    captured = capfd.readouterr()
    assert captured.out == "" and captured.err == ""
