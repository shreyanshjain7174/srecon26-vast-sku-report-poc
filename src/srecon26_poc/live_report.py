"""Exact-target desktop-browser report adapter for confirmed provider faults only."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import urlsplit

from .reporting import FaultRecord, ReportReceipt
from .vast_provider import VastCliProvider


class LiveBrowserReportError(ValueError):
    pass


Runner = Callable[[Sequence[str], int], str]


def _run(arguments: Sequence[str], timeout: int) -> str:
    try:
        return subprocess.run(list(arguments), check=True, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise LiveBrowserReportError("desktop browser report command failed safely") from error


class LiveBrowserReportAdapter:
    """Drive the already-running desktop browser, never a fixture page.

    The adapter refuses to click unless the authenticated Instances page
    contains exactly one DOM row whose visible text includes both the numeric
    instance ID and the nonce-bound label, and that row exposes exactly one
    control whose accessible text contains ``report``.
    """

    def __init__(self, browser_bin: Path, vast_cli: Path | str, evidence_dir: Path, *, runner: Runner = _run) -> None:
        if not browser_bin.is_file() or not os.access(browser_bin, os.X_OK):
            raise LiveBrowserReportError("desktop browser binary is unavailable")
        self.browser_bin = str(browser_bin)
        self.provider = VastCliProvider(vast_cli, runner=lambda arguments, timeout: runner(arguments, timeout))
        self.evidence_dir = evidence_dir
        self.runner = runner

    def _browser(self, *arguments: str, timeout: int = 20) -> str:
        return self.runner([self.browser_bin, *arguments], timeout)

    def preflight_authenticated_session(self) -> None:
        """Prove the report page is usable before any paid create."""

        self._browser("goto", "https://cloud.vast.ai/instances/", timeout=20)
        current_url = self._browser("url", timeout=20).strip()
        visible = self._browser("text", timeout=20)
        parsed = urlsplit(current_url)
        exact_instances_page = parsed.scheme == "https" and parsed.netloc == "cloud.vast.ai" and parsed.path in {"/instances", "/instances/"} and not parsed.query and not parsed.fragment
        normalized = " ".join(visible.casefold().split())
        unauthenticated = any(marker in normalized for marker in ("login", "log in", "sign in"))
        if not exact_instances_page or unauthenticated or "instances" not in normalized:
            raise LiveBrowserReportError("desktop browser is not authenticated on the Vast instances page")

    def preflight_exact_instance(self, instance_id: int, label: str) -> None:
        current = self.provider.get_instance(instance_id)
        if current.label != label:
            raise LiveBrowserReportError("provider instance no longer has the exact nonce-bound label")
        self._browser("goto", "https://cloud.vast.ai/instances/", timeout=20)
        visible = self._browser("text", timeout=20)
        if "Login" in visible or str(instance_id) not in visible or label not in visible:
            raise LiveBrowserReportError("authenticated desktop page does not expose the exact report target")

    def capture_before(self, fault: FaultRecord) -> Path:
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        path = self.evidence_dir / f"before-instance-{fault.instance_id}.png"
        self._browser("screenshot", str(path), timeout=20)
        return path

    def submit(self, fault: FaultRecord) -> bool:
        instance_id = json.dumps(str(fault.instance_id))
        label = json.dumps(fault.label)
        script = f"""(() => {{
          const id = {instance_id}; const label = {label};
          const rows = [...document.querySelectorAll('tr,[role=row],article,li,div')]
            .filter(el => {{ const t=(el.innerText||'').trim(); return t.includes(id) && t.includes(label); }});
          const minimal = rows.filter(el => !rows.some(other => other !== el && el.contains(other)));
          if (minimal.length !== 1) throw new Error('exact report target row is not unique');
          const controls = [...minimal[0].querySelectorAll('button,a,[role=button]')]
            .filter(el => /report/i.test((el.innerText||el.getAttribute('aria-label')||el.title||'').trim()));
          if (controls.length !== 1) throw new Error('exact report control is not unique');
          controls[0].click(); return 'clicked-exact-report-control';
        }})()"""
        self._browser("js", script, timeout=20)
        confirmation = self._browser("text", timeout=20).lower()
        return any(marker in confirmation for marker in ("report submitted", "report received", "thanks for reporting", "reported"))

    def capture_after(self, receipt: ReportReceipt) -> Path:
        path = self.evidence_dir / ("after-report-confirmed.png" if receipt.confirmed else "after-report-unconfirmed.png")
        self._browser("screenshot", str(path), timeout=20)
        return path


def create_adapter() -> LiveBrowserReportAdapter:
    def required(name: str) -> str:
        value = os.environ.get(name)
        if not value:
            raise LiveBrowserReportError(f"{name} is required")
        return value

    adapter = LiveBrowserReportAdapter(
        Path(required("SRECON26_BROWSER_BIN")).expanduser(),
        required("SRECON26_VAST_CLI"),
        Path(required("SRECON26_REPORT_EVIDENCE_DIR")).expanduser().resolve(),
    )
    adapter.preflight_authenticated_session()
    return adapter
