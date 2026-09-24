"""Disposable Ubuntu-root installation check; opt in only inside a container.

This test writes standard guard paths and stubs systemctl activation. Never opt
in on a controller or workstation. Unit tests run everywhere without this flag.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


UTC = timezone.utc


pytestmark = pytest.mark.skipif(
    os.environ.get("SRECON26_TEST_UBUNTU_INSTALLER") != "1",
    reason="requires opt-in disposable Ubuntu root container",
)


def test_ubuntu_installer_forced_commands_permissions_and_units():
    assert os.geteuid() == 0 and Path("/.dockerenv").is_file()
    assert 'ID=ubuntu' in Path("/etc/os-release").read_text()
    assert not Path("/etc/srecon26-guard").exists()
    source = Path(__file__).resolve().parents[2] / "guard"
    with tempfile.TemporaryDirectory(prefix="guard-test-", dir="/root") as temporary:
        staging = Path(temporary)
        shutil.copytree(source, staging / "guard")
        binary = Path("/opt/test-vast/bin/vastai")
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/usr/bin/python3\nprint('[]')\n")
        binary.chmod(0o755)
        config = staging / "controller.json"
        config.write_text(json.dumps({
            "azure_resource_id": "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/guard-rg/providers/Microsoft.Compute/virtualMachines/guard-vm",
            "azure_vm_id": "22222222-2222-2222-2222-222222222222",
            "vast_bin": str(binary),
        }))
        config.chmod(0o600)
        Path("/etc/ssh").mkdir(exist_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", "/etc/ssh/ssh_host_ed25519_key"], check=True)
        # Containers do not run systemd as PID1. Record activation requests;
        # separately validate real systemd's unit/calendar parsers below.
        systemctl = Path("/usr/bin/systemctl")
        systemctl.write_text(
            "#!/usr/bin/python3\nimport pathlib,sys\n"
            "with pathlib.Path('/root/guard-systemctl.log').open('a') as f: f.write(' '.join(sys.argv[1:])+'\\n')\n"
            "if sys.argv[1] == 'show': print('LoadState=loaded\\nActiveState=active\\nUnitFileState=enabled')\n"
        )
        systemctl.chmod(0o755)
        installer = ["bash", str(staging / "guard/install_azure_guard.sh"), str(config)]
        config.chmod(0o644)
        unsafe = subprocess.run(installer, capture_output=True, text=True)
        assert unsafe.returncode == 2 and not Path("/etc/srecon26-guard").exists()
        config.chmod(0o600)
        installed = subprocess.run(installer, capture_output=True, text=True)
        assert installed.returncode == 0, installed.stderr
        calls = Path("/root/guard-systemctl.log").read_text().splitlines()
        assert calls == ["daemon-reload", "enable --now srecon26-azure-guard.timer", "is-enabled --quiet srecon26-azure-guard.timer", "is-active --quiet srecon26-azure-guard.timer"]
        assert not Path("/etc/srecon26-guard/vast-api-key").exists()
        subprocess.run(["systemd-analyze", "verify", "/etc/systemd/system/srecon26-azure-guard.service", "/etc/systemd/system/srecon26-azure-guard.timer"], check=True, capture_output=True)
        calendar = subprocess.run(["systemd-analyze", "calendar", "--iterations=4", "*-*-* *:*:00,15,30,45"], check=True, capture_output=True, text=True)
        assert "Normalized form:" in calendar.stdout

        def rpc(args, original, data):
            environment = {"PATH": "/usr/bin:/bin", "PYTHONPATH": "/untrusted", "VAST_API_KEY": "ignored-environment-sentinel"}
            if original is not None:
                environment["SSH_ORIGINAL_COMMAND"] = original
            return subprocess.run(["/usr/local/sbin/guardctl", *args], input=data, capture_output=True, env=environment)

        secret_value = b"private-fixture-sentinel\n"
        bootstrap = rpc(["install-credential"], "guardctl install-credential", secret_value)
        assert bootstrap.returncode == 0 and not bootstrap.stderr
        assert json.loads(bootstrap.stdout)["status"] == "CREDENTIAL_INSTALLED"
        secret = Path("/etc/srecon26-guard/vast-api-key")
        assert secret.stat().st_uid == 0 and secret.stat().st_mode & 0o777 == 0o600
        assert secret.read_bytes() == secret_value
        unprivileged = subprocess.run(["runuser", "-u", "nobody", "--", "/usr/bin/python3", "-c", "from pathlib import Path; Path('/etc/srecon26-guard/vast-api-key').read_bytes()"], capture_output=True)
        assert unprivileged.returncode != 0 and secret_value.strip() not in unprivileged.stderr + unprivileged.stdout
        for args, original in [([], "guardctl install-credential"), (["install-credential"], "guardctl"), (["tick-all"], "guardctl tick-all"), (["install-credential"], "guardctl install-credential")]:
            refused = rpc(args, original, b"replacement\n")
            assert refused.returncode == 2 and not refused.stderr
            assert secret_value.strip() not in refused.stdout and b"replacement" not in refused.stdout
        assert secret.read_bytes() == secret_value
        nonce = "container_nonce_123"
        arm = {
            "run_id": "container-test", "nonce": nonce, "label": f"container-test--nonce-{nonce}",
            "hard_deadline": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            "heartbeat_timeout_seconds": 45,
        }
        def request(command, payload):
            return rpc([], "guardctl", json.dumps({"protocol": "srecon26-guard-v1", "command": command, "payload": payload}).encode())
        armed = request("arm", arm)
        assert armed.returncode == 0 and not armed.stderr, armed.stdout
        assert json.loads(armed.stdout)["status"] == "ARMED"
        assert request("preflight", {}).returncode == 0
        tick = rpc(["tick-all"], None, b"")
        assert tick.returncode == 0 and not tick.stderr
        status = json.loads(request("status", {"nonce": nonce}).stdout)
        exported = request("export", {"nonce": nonce, "root_hash": status["root_hash"]})
        assert exported.returncode == 0 and not exported.stderr
        assert secret_value.strip() not in exported.stdout and b"ignored-environment-sentinel" not in exported.stdout
        Path("/etc/srecon26-guard").chmod(0o755)
        refused = subprocess.run(installer, capture_output=True)
        assert refused.returncode == 2
        assert Path("/etc/srecon26-guard").stat().st_mode & 0o777 == 0o755
