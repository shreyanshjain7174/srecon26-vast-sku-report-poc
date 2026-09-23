from __future__ import annotations

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator, Mapping

from .journal import InvalidJournal, RunJournal


MAX_EXPOSURE = Decimal("5.00")
CATEGORY_CAPS = {
    "gpu-smoke": Decimal("1.00"),
    # Up to four separately bounded direct-inference attempts.  The fourth is
    # reserved for a contract correction proven by retained live evidence.
    # This is not
    # a retry of (or prerequisite for) the KVM smoke entitlement.
    "gpu-inference-smoke": Decimal("4.40"),
    # One durable retry entitlement while exactly one original smoke invoice
    # remains pending.  It cannot be split across multiple retry reservations.
    "gpu-smoke-retry": Decimal("0.90"),
    # One final, independently reviewed smoke attempt on a mechanically
    # verified distinct machine after the first retry has settled.
    "gpu-smoke-distinct-machine": Decimal("0.25"),
    "canary": Decimal("1.00"),
    "paired-comparison": Decimal("3.00"),
}

# One reviewed, hash-pinned replacement entitlement for a preserved run that failed before
# provider.create_intent.  The canonical journal is hash-pinned so a different
# local timeout cannot silently expand the paid-attempt envelope.
INFERENCE_REPLACEMENT_RUN_ID = "inference-infer202609231456"
INFERENCE_REPLACEMENT_JOURNAL_SHA256 = "0a66d7bd0ad2693db76a0e02327ed9a1c9ff61425c792130a2191742979407be"
INFERENCE_REPLACEMENT_EVIDENCE_SHA256 = "1c193985949a91adfaa7646de3cf9afb8f3aaa1c0e257fefbb7d28f10466d927"
INFERENCE_REPLACEMENT_EVIDENCE_PATH = Path(__file__).resolve().parents[2] / "evidence" / "inference-infer202609231456-replacement-entitlement.json"
INFERENCE_MEASUREMENT_RETRY_RUN_ID = "inference-infer20260923150917"
INFERENCE_MEASUREMENT_RETRY_JOURNAL_SHA256 = "932c6bcc7d3babb71a9d0b9642575815610210e11ab7cdd3a09360273325080e"
INFERENCE_MEASUREMENT_RETRY_EVIDENCE_SHA256 = "bfefa7f8548cf0766d1f843b4cc3443408c6c09ceb46e01dde30fe7a0decb7f9"
INFERENCE_MEASUREMENT_RETRY_EVIDENCE_PATH = Path(__file__).resolve().parents[2] / "evidence" / "inference-infer20260923150917-measurement-retry-entitlement.json"
INFERENCE_STARTUP_RETRY_RUN_ID = "inference-infer20260923154131"
INFERENCE_STARTUP_RETRY_JOURNAL_SHA256 = "f1f384770f2962efbbd9814d5f3f7ebbd7d1ad1a6469b03e5da3e7ec7abe0c23"
INFERENCE_STARTUP_RETRY_EVIDENCE_SHA256 = "b04821f538dba046d791e08002de2748efe5f9bfd7a7f629d0af3bc0e4ac2565"
INFERENCE_STARTUP_RETRY_EVIDENCE_PATH = Path(__file__).resolve().parents[2] / "evidence" / "inference-infer20260923154131-startup-retry-entitlement.json"
INFERENCE_REPORT_SESSION_RETRY_RUN_ID = "inference-infer20260923174935"
INFERENCE_REPORT_SESSION_RETRY_JOURNAL_SHA256 = "23b52819f536ccf84f7cc90fbdf554f038a0e4e6db3adae77c7d927365f70fed"
INFERENCE_REPORT_SESSION_RETRY_EVIDENCE_SHA256 = "718e750bcc09e0a00016f4c24c1867652134fd7cb9a3545d7691060e2bcf7920"
INFERENCE_REPORT_SESSION_RETRY_EVIDENCE_PATH = Path(__file__).resolve().parents[2] / "evidence" / "inference-infer20260923174935-report-session-retry-entitlement.json"
ALTERNATE_TEMPLATE_HASH = "10d921fdff3c0d2a794897d81ae870c5"
ALTERNATE_TEMPLATE_IMAGE = "docker.io/vastai/kvm:ubuntu_desktop_22.04-2025-11-21"
ALTERNATE_TEMPLATE_EVIDENCE_PATH = Path(__file__).resolve().parents[2] / "evidence" / "vast-template-ubuntu-desktop-vm-20260923.json"
ALTERNATE_TEMPLATE_EVIDENCE_SHA256 = "771fd7af957052ea991a29c95f8f61d59124a23d7fa8f4a444c17dd4985b48ca"
ALTERNATE_TEMPLATE_LIVE_FAILURE_RUN_ID = "inference-infer20260923175832"
PRIMARY_TEMPLATE_HASH = "b7942f6bbc4374893ff66eb78145bbac"
PRIMARY_TEMPLATE_IMAGE = "docker.io/vastai/kvm:ubuntu_cli_22.04-2025-05-16"
PRIMARY_TEMPLATE_TERMINAL_FAILURE_RUN_ID = "inference-infer20260923180850"
INFERENCE_RESERVATION_REJECTION_RUN_ID = "inference-infer20260923182103"
INFERENCE_RESERVATION_REJECTION_JOURNAL_SHA256 = "f235d618103c257bff82a58b9341c60c3e08e539e88522ce2e5a45c091b6b729"
INFERENCE_RESERVATION_REJECTION_EVIDENCE_PATH = Path(__file__).resolve().parents[2] / "evidence" / "inference-infer20260923182103-reservation-rejection-retry-entitlement.json"
INFERENCE_RESERVATION_REJECTION_EVIDENCE_SHA256 = "352df74268dfa9e4ca32c3c7a02a8bf92d6cd22c85d256da6646fb68ea4203b9"


class BudgetExceeded(ValueError):
    pass


class BudgetWriteFailure(RuntimeError):
    pass


class ExposureLedger:
    """Lock-protected Decimal-only exposure reservations persisted atomically."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _read(self) -> dict[str, object]:
        if not self.path.exists():
            return {"reservations": {}}
        try:
            data = json.loads(self.path.read_text(), parse_float=Decimal)
            if not isinstance(data, dict) or not isinstance(data.get("reservations"), dict):
                raise ValueError("invalid ledger schema")
            for run_id, entry in data["reservations"].items():
                if not isinstance(run_id, str) or not run_id or not isinstance(entry, Mapping):
                    raise ValueError("invalid reservation entry")
                self._validate_entry(run_id, entry)
            return data
        except (OSError, ValueError, TypeError, KeyError, InvalidOperation, json.JSONDecodeError) as exc:
            raise BudgetWriteFailure("ledger cannot be read safely") from exc

    def _write(self, data: Mapping[str, object]) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode() + b"\n"
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, encoded)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temporary, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise BudgetWriteFailure("ledger write failed") from exc

    @staticmethod
    def _decimal(value: object) -> Decimal:
        if isinstance(value, Decimal):
            return value
        if isinstance(value, (str, int)):
            return Decimal(str(value))
        raise TypeError("budget values must be Decimal, string, or integer; float is forbidden")

    def _evidence_path(self, value: object) -> Path:
        if not isinstance(value, str) or not value or Path(value).is_absolute() or ".." in Path(value).parts:
            raise ValueError("invalid evidence artifact path")
        root = self.path.parent.resolve()
        resolved = (root / value).resolve()
        if root not in resolved.parents:
            raise ValueError("evidence artifact escapes ledger root")
        return resolved

    def _validate_entry(self, run_id: str, entry: Mapping[str, object]) -> None:
        amount = self._decimal(entry["amount"])
        category = entry["category"]
        actual = entry.get("actual")
        proof = entry.get("absence_proof")
        if not amount.is_finite() or amount <= 0 or category not in CATEGORY_CAPS:
            raise ValueError("invalid reservation amount or category")
        machine_id = entry.get("machine_id")
        if machine_id is not None and (
            category != "gpu-inference-smoke"
            or isinstance(machine_id, bool)
            or not isinstance(machine_id, int)
            or machine_id <= 0
        ):
            raise ValueError("invalid inference machine reservation binding")
        template_hash = entry.get("template_hash")
        image_contract = entry.get("image_contract")
        if (template_hash is not None or image_contract is not None) and (
            category != "gpu-inference-smoke"
            or not isinstance(template_hash, str)
            or not isinstance(image_contract, str)
        ):
            raise ValueError("invalid inference template reservation binding")
        if actual is None and proof is None:
            return
        if actual is None or not isinstance(proof, Mapping) or proof.get("reads") != 3:
            raise ValueError("finalized reservation requires actual spend and three-read absence proof")
        actual_value = self._decimal(actual)
        billing = proof.get("billing")
        instance_id = proof.get("instance_id")
        label = proof.get("label")
        if not actual_value.is_finite() or actual_value < 0 or actual_value > amount or not isinstance(billing, Mapping):
            raise ValueError("invalid finalized reservation")
        if isinstance(instance_id, bool) or not isinstance(instance_id, int) or instance_id <= 0 or not isinstance(label, str) or not label:
            raise ValueError("finalized reservation requires exact instance identity")
        if billing.get("source") != "provider_invoice":
            raise ValueError("finalized reservation requires provider invoice evidence")
        artifact = self._evidence_path(billing.get("artifact"))
        expected_hash = billing.get("sha256")
        if not isinstance(expected_hash, str) or len(expected_hash) != 64:
            raise ValueError("invalid provider invoice artifact binding")
        raw = artifact.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_hash:
            raise ValueError("provider invoice artifact hash mismatch")
        invoice = json.loads(raw, parse_float=Decimal)
        if not isinstance(invoice, Mapping):
            raise ValueError("invalid provider invoice artifact")
        if invoice.get("schema") != "srecon26-vast-invoice-evidence/v1" or invoice.get("source") != "VastCliProvider.capture_invoice_charge/v1":
            raise ValueError("provider invoice artifact has an untrusted source")
        if invoice.get("run_id") != run_id or invoice.get("instance_id") != instance_id or invoice.get("label") != label:
            raise ValueError("provider invoice is not bound to the reserved run")
        provider_charge = invoice.get("provider_charge")
        metadata = provider_charge.get("metadata") if isinstance(provider_charge, Mapping) else None
        if not isinstance(provider_charge, Mapping) or provider_charge.get("source") != f"instance-{instance_id}" or provider_charge.get("type") != "instance" or not isinstance(metadata, Mapping) or metadata.get("label") != label:
            raise ValueError("provider invoice does not identify the exact instance")
        if self._decimal(invoice.get("amount_usd")) != actual_value or self._decimal(provider_charge.get("amount")) != actual_value:
            raise ValueError("provider invoice amount does not match actual spend")
        if not isinstance(invoice.get("observed_at"), str) or not str(invoice["observed_at"]).endswith("Z"):
            raise ValueError("provider invoice timestamp is missing")
        command = invoice.get("command")
        if not isinstance(command, list) or command[:3] != ["show", "invoices-v1", "--charges"] or "--verbose" not in command:
            raise ValueError("provider invoice capture command is invalid")

        journal_path = self._evidence_path(billing.get("journal_artifact"))
        journal_hash = billing.get("journal_sha256")
        if journal_path.name != "journal.ndjson" or not isinstance(journal_hash, str) or hashlib.sha256(journal_path.read_bytes()).hexdigest() != journal_hash:
            raise ValueError("run journal artifact hash mismatch")
        try:
            journal = RunJournal.open(journal_path.parent)
        except InvalidJournal as error:
            raise ValueError("run journal is invalid") from error
        creates = [event for event in journal.events() if event.event_type == "provider.create_observed"]
        if len(creates) != 1 or creates[0].run_id != run_id or creates[0].label != label or creates[0].payload.get("instance_id") != instance_id:
            raise ValueError("provider invoice does not match the immutable create journal")
        teardowns = [event for event in journal.events() if event.event_type == "teardown.exact"]
        if len(teardowns) != 1 or teardowns[0].payload.get("instance_id") != instance_id or teardowns[0].payload.get("label") != label or teardowns[0].sequence <= creates[0].sequence or journal.state().value != "TERMINAL":
            raise ValueError("run journal does not prove exact teardown before terminal state")

        absence_path = self._evidence_path(billing.get("absence_artifact"))
        absence_hash = billing.get("absence_sha256")
        absence_raw = absence_path.read_bytes()
        if not isinstance(absence_hash, str) or hashlib.sha256(absence_raw).hexdigest() != absence_hash:
            raise ValueError("provider absence artifact hash mismatch")
        absence = json.loads(absence_raw)
        if not isinstance(absence, Mapping) or absence.get("schema") != "srecon26-vast-absence-evidence/v1" or absence.get("source") != "VastCliProvider.capture_absence_evidence/v1":
            raise ValueError("provider absence artifact has an untrusted source")
        if absence.get("run_id") != run_id or absence.get("instance_id") != instance_id or absence.get("label") != label:
            raise ValueError("provider absence artifact is not bound to the exact run")
        reads = absence.get("reads")
        if not isinstance(reads, list) or len(reads) != 3 or any(not isinstance(read, Mapping) or read.get("matching_instances") != 0 or not isinstance(read.get("observed_at"), str) for read in reads):
            raise ValueError("provider absence artifact does not contain three zero-match reads")

    def _entry_exposure(self, run_id: str, entry: Mapping[str, object]) -> Decimal:
        self._validate_entry(run_id, entry)
        actual = entry.get("actual")
        if actual is not None:
            return self._decimal(actual)
        return self._decimal(entry["amount"])

    def _exposure(self, data: Mapping[str, object]) -> Decimal:
        reservations = data["reservations"]
        assert isinstance(reservations, Mapping)
        return sum((self._entry_exposure(run_id, entry) for run_id, entry in reservations.items() if isinstance(run_id, str) and isinstance(entry, Mapping)), Decimal("0.00"))

    def _category_exposure(self, data: Mapping[str, object], category: str) -> Decimal:
        reservations = data["reservations"]
        assert isinstance(reservations, Mapping)
        return sum(
            (
                self._entry_exposure(run_id, entry)
                for run_id, entry in reservations.items()
                if isinstance(entry, Mapping)
                and entry.get("category") == category
            ),
            Decimal("0.00"),
        )

    def _has_pinned_inference_replacement_entitlement(self, reservations: Mapping[str, object]) -> bool:
        entry = reservations.get(INFERENCE_REPLACEMENT_RUN_ID)
        if not isinstance(entry, Mapping) or entry.get("category") != "gpu-inference-smoke":
            return False
        journal_path = self.path.parent / INFERENCE_REPLACEMENT_RUN_ID / "journal" / "journal.ndjson"
        evidence_path = INFERENCE_REPLACEMENT_EVIDENCE_PATH
        try:
            if hashlib.sha256(journal_path.read_bytes()).hexdigest() != INFERENCE_REPLACEMENT_JOURNAL_SHA256:
                return False
            journal = RunJournal.open(journal_path.parent)
            evidence_raw = evidence_path.read_bytes()
            if hashlib.sha256(evidence_raw).hexdigest() != INFERENCE_REPLACEMENT_EVIDENCE_SHA256:
                return False
            evidence = json.loads(evidence_raw)
        except (OSError, InvalidJournal, json.JSONDecodeError):
            return False
        events = journal.events()
        expected = "external desktop did not answer preflight-authenticated-session within 60 seconds"
        terminal = [event for event in events if event.event_type == "terminal.safe"]
        reads = evidence.get("provider_absence_reads") if isinstance(evidence, Mapping) else None
        return (
            bool(events)
            and all(event.run_id == INFERENCE_REPLACEMENT_RUN_ID for event in events)
            and journal.state().value == "TERMINAL"
            and not any(event.event_type in {"provider.create_intent", "provider.create_observed"} for event in events)
            and len(terminal) == 1
            and terminal[0].payload.get("limitation") == expected
            and terminal[0].payload.get("status") == "FAILED_SAFE"
            and evidence.get("schema") == "srecon26-inference-replacement-entitlement/v1"
            and evidence.get("run_id") == INFERENCE_REPLACEMENT_RUN_ID
            and evidence.get("journal_sha256") == INFERENCE_REPLACEMENT_JOURNAL_SHA256
            and evidence.get("provider_command") == ["show", "instances", "--raw"]
            and isinstance(reads, list)
            and len(reads) == 3
            and all(isinstance(read, Mapping) and read.get("matching_instances") == 0 and read.get("raw") == [] for read in reads)
        )

    def _has_pinned_measurement_retry_entitlement(self, reservations: Mapping[str, object]) -> bool:
        entry = reservations.get(INFERENCE_MEASUREMENT_RETRY_RUN_ID)
        if not isinstance(entry, Mapping) or entry.get("category") != "gpu-inference-smoke":
            return False
        journal_path = self.path.parent / INFERENCE_MEASUREMENT_RETRY_RUN_ID / "journal" / "journal.ndjson"
        try:
            if hashlib.sha256(journal_path.read_bytes()).hexdigest() != INFERENCE_MEASUREMENT_RETRY_JOURNAL_SHA256:
                return False
            journal = RunJournal.open(journal_path.parent)
            evidence_raw = INFERENCE_MEASUREMENT_RETRY_EVIDENCE_PATH.read_bytes()
            if hashlib.sha256(evidence_raw).hexdigest() != INFERENCE_MEASUREMENT_RETRY_EVIDENCE_SHA256:
                return False
            evidence = json.loads(evidence_raw)
        except (OSError, InvalidJournal, json.JSONDecodeError):
            return False
        events = journal.events()
        creates = [event for event in events if event.event_type == "provider.create_observed"]
        teardowns = [event for event in events if event.event_type == "teardown.exact"]
        absence = [event for event in events if event.event_type == "absence.proved"]
        terminal = [event for event in events if event.event_type == "terminal.safe"]
        warmup = evidence.get("warmup_observation") if isinstance(evidence, Mapping) else None
        return (
            bool(events)
            and all(event.run_id == INFERENCE_MEASUREMENT_RETRY_RUN_ID for event in events)
            and journal.state().value == "TERMINAL"
            and len(creates) == 1
            and len(teardowns) == 1
            and len(absence) == 1
            and absence[0].payload.get("reads") == 3
            and len(terminal) == 1
            and terminal[0].payload.get("status") == "FAILED_SAFE"
            and evidence.get("schema") == "srecon26-inference-measurement-retry-entitlement/v1"
            and evidence.get("run_id") == INFERENCE_MEASUREMENT_RETRY_RUN_ID
            and evidence.get("journal_sha256") == INFERENCE_MEASUREMENT_RETRY_JOURNAL_SHA256
            and evidence.get("provider_create_calls") == 1
            and evidence.get("provider_destroy_calls") == 1
            and evidence.get("absence_reads") == 3
            and evidence.get("report_attempted") is False
            and evidence.get("actual_usd") == "0.133"
            and isinstance(warmup, Mapping)
            and warmup.get("http_code") == 200
            and warmup.get("model") == "Qwen/Qwen2.5-1.5B-Instruct"
            and warmup.get("ttft_method") == "curl_time_starttransfer_first_stream_response_byte"
        )

    def _has_pinned_startup_retry_entitlement(self, reservations: Mapping[str, object]) -> bool:
        entry = reservations.get(INFERENCE_STARTUP_RETRY_RUN_ID)
        if (
            not isinstance(entry, Mapping)
            or entry.get("category") != "gpu-inference-smoke"
            or self._decimal(entry.get("actual")) != Decimal("0.008")
            or not isinstance(entry.get("absence_proof"), Mapping)
        ):
            return False
        journal_path = self.path.parent / INFERENCE_STARTUP_RETRY_RUN_ID / "journal" / "journal.ndjson"
        run_path = journal_path.parents[1]
        status_path = self.path.parent / INFERENCE_STARTUP_RETRY_RUN_ID / "remote-transport" / "provider-status.ndjson"
        try:
            if hashlib.sha256(journal_path.read_bytes()).hexdigest() != INFERENCE_STARTUP_RETRY_JOURNAL_SHA256:
                return False
            journal = RunJournal.open(journal_path.parent)
            evidence_raw = INFERENCE_STARTUP_RETRY_EVIDENCE_PATH.read_bytes()
            if hashlib.sha256(evidence_raw).hexdigest() != INFERENCE_STARTUP_RETRY_EVIDENCE_SHA256:
                return False
            evidence = json.loads(evidence_raw)
            status_raw = status_path.read_bytes()
            status_records = [json.loads(line) for line in status_raw.splitlines() if line]
            if not status_records or any(not isinstance(record, Mapping) for record in status_records):
                return False
            provider_reads = [record for record in status_records if "event" not in record]
            sums_raw = (run_path / "SHA256SUMS").read_bytes()
            root_raw = (run_path / "ROOT-HASH.txt").read_bytes()
            root_hash = root_raw.decode("utf-8").strip()
            if hashlib.sha256(sums_raw).hexdigest() != root_hash:
                return False
            sealed: dict[str, str] = {}
            for raw_line in sums_raw.decode("utf-8").splitlines():
                digest, relative = raw_line.split("  ", 1)
                artifact = (run_path / relative).resolve()
                if run_path.resolve() not in artifact.parents or hashlib.sha256(artifact.read_bytes()).hexdigest() != digest:
                    return False
                sealed[relative] = digest
            anchor_raw = (run_path / "guard-anchor.json").read_bytes()
            anchor = json.loads(anchor_raw)
            pre_anchor_sums = (run_path / "pre-anchor" / "SHA256SUMS").read_bytes()
            pre_anchor_root = (run_path / "pre-anchor" / "ROOT-HASH.txt").read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError, InvalidJournal, json.JSONDecodeError, ValueError):
            return False
        events = journal.events()
        creates = [event for event in events if event.event_type == "provider.create_observed"]
        starts = [event for event in events if event.event_type == "canary.started"]
        teardowns = [event for event in events if event.event_type == "teardown.exact"]
        absence = [event for event in events if event.event_type == "absence.proved"]
        terminal = [event for event in events if event.event_type == "terminal.safe"]
        return (
            bool(status_records)
            and all(event.run_id == INFERENCE_STARTUP_RETRY_RUN_ID for event in events)
            and journal.state().value == "TERMINAL"
            and len(creates) == 1
            and len(starts) == 1
            and len(teardowns) == 1
            and len(absence) == 1
            and absence[0].payload.get("reads") == 3
            and len(terminal) == 1
            and terminal[0].payload.get("status") == "FAILED_SAFE"
            and evidence.get("schema") == "srecon26-inference-startup-retry-entitlement/v1"
            and evidence.get("run_id") == INFERENCE_STARTUP_RETRY_RUN_ID
            and evidence.get("journal_sha256") == INFERENCE_STARTUP_RETRY_JOURNAL_SHA256
            and evidence.get("sealed_root") == root_hash
            and evidence.get("root_hash_file_sha256") == hashlib.sha256(root_raw).hexdigest()
            and evidence.get("guard_anchor_sha256") == hashlib.sha256(anchor_raw).hexdigest()
            and sealed.get("guard-anchor.json") == evidence.get("guard_anchor_sha256")
            and sealed.get("remote-transport/provider-status.ndjson") == evidence.get("provider_status_sha256")
            and sealed.get("remote-transport/failure.txt") == hashlib.sha256(b"exact instance did not reach running with a safe SSH endpoint within the bounded wait\n").hexdigest()
            and anchor.get("acknowledged") == pre_anchor_root
            and anchor.get("root_hash") == pre_anchor_root
            and hashlib.sha256(pre_anchor_sums).hexdigest() == pre_anchor_root
            and evidence.get("provider_status_sha256") == hashlib.sha256(status_raw).hexdigest()
            and evidence.get("provider_status_last_attempt") == 60
            and evidence.get("provider_status_reached_running") is False
            and len(provider_reads) == 60
            and [record.get("attempt") for record in provider_reads] == list(range(1, 61))
            and all(record.get("instance_id") == 52254086 for record in provider_reads)
            and all(record.get("label") == "srecon26-inference--nonce-8e886e93ecf9943ff9e70232" for record in provider_reads)
            and not any(record.get("actual_status") == "running" for record in provider_reads)
            and evidence.get("provider_create_calls") == 1
            and evidence.get("provider_destroy_calls") == 1
            and evidence.get("absence_reads") == 3
            and evidence.get("report_attempted") is False
            and evidence.get("actual_usd") == "0.008"
        )

    def _has_pinned_report_session_retry_entitlement(self, reservations: Mapping[str, object]) -> bool:
        entry = reservations.get(INFERENCE_REPORT_SESSION_RETRY_RUN_ID)
        if not isinstance(entry, Mapping) or entry.get("category") != "gpu-inference-smoke":
            return False
        run_path = self.path.parent / INFERENCE_REPORT_SESSION_RETRY_RUN_ID
        journal_path = run_path / "journal" / "journal.ndjson"
        try:
            if hashlib.sha256(journal_path.read_bytes()).hexdigest() != INFERENCE_REPORT_SESSION_RETRY_JOURNAL_SHA256:
                return False
            journal = RunJournal.open(journal_path.parent)
            evidence_raw = INFERENCE_REPORT_SESSION_RETRY_EVIDENCE_PATH.read_bytes()
            if hashlib.sha256(evidence_raw).hexdigest() != INFERENCE_REPORT_SESSION_RETRY_EVIDENCE_SHA256:
                return False
            evidence = json.loads(evidence_raw)
            request_paths = list((run_path / "desktop-report").glob(".external-report-*/01-preflight-authenticated-session.request.json"))
            if len(request_paths) != 1:
                return False
        except (OSError, InvalidJournal, json.JSONDecodeError):
            return False
        events = journal.events()
        terminal = [event for event in events if event.event_type == "terminal.safe"]
        reads = evidence.get("provider_absence_reads") if isinstance(evidence, Mapping) else None
        expected = "external desktop did not answer preflight-authenticated-session within 60 seconds"
        return (
            bool(events)
            and all(event.run_id == INFERENCE_REPORT_SESSION_RETRY_RUN_ID for event in events)
            and journal.state().value == "TERMINAL"
            and not any(event.event_type in {"provider.create_intent", "provider.create_observed"} for event in events)
            and len(terminal) == 1
            and terminal[0].payload.get("limitation") == expected
            and terminal[0].payload.get("status") == "FAILED_SAFE"
            and evidence.get("schema") == "srecon26-inference-report-session-retry-entitlement/v1"
            and evidence.get("run_id") == INFERENCE_REPORT_SESSION_RETRY_RUN_ID
            and evidence.get("journal_sha256") == INFERENCE_REPORT_SESSION_RETRY_JOURNAL_SHA256
            and evidence.get("authenticated_session_request_sha256") == hashlib.sha256(request_paths[0].read_bytes()).hexdigest()
            and evidence.get("provider_command") == ["show", "instances", "--raw"]
            and isinstance(reads, list)
            and len(reads) == 3
            and all(isinstance(read, Mapping) and read.get("matching_instances") == 0 and read.get("raw") == [] for read in reads)
        )

    @staticmethod
    def _has_alternate_template_authorization(template_hash: str | None, image_contract: str | None) -> bool:
        if template_hash != ALTERNATE_TEMPLATE_HASH or image_contract != ALTERNATE_TEMPLATE_IMAGE:
            return False
        try:
            raw = ALTERNATE_TEMPLATE_EVIDENCE_PATH.read_bytes()
            evidence = json.loads(raw)
            template = evidence.get("template") if isinstance(evidence, Mapping) else None
        except (OSError, json.JSONDecodeError):
            return False
        return (
            hashlib.sha256(raw).hexdigest() == ALTERNATE_TEMPLATE_EVIDENCE_SHA256
            and evidence.get("schema") == "srecon26-vast-template-snapshot/v1"
            and isinstance(template, Mapping)
            and template.get("hash_id") == template_hash
            and f"{template.get('image')}:{template.get('tag')}" == image_contract
            and template.get("vm") is True
            and template.get("ssh_direct") is True
        )

    def _has_settled_alternate_template_failure(self, reservations: Mapping[str, object]) -> bool:
        entry = reservations.get(ALTERNATE_TEMPLATE_LIVE_FAILURE_RUN_ID)
        proof = entry.get("absence_proof") if isinstance(entry, Mapping) else None
        return (
            isinstance(entry, Mapping)
            and entry.get("category") == "gpu-inference-smoke"
            and self._decimal(entry.get("actual")) == Decimal("0.017")
            and entry.get("machine_id") == 55957
            and entry.get("template_hash") == ALTERNATE_TEMPLATE_HASH
            and entry.get("image_contract") == ALTERNATE_TEMPLATE_IMAGE
            and isinstance(proof, Mapping)
            and proof.get("reads") == 3
        )

    def _has_settled_terminal_startup_failure(self, reservations: Mapping[str, object]) -> bool:
        entry = reservations.get(PRIMARY_TEMPLATE_TERMINAL_FAILURE_RUN_ID)
        proof = entry.get("absence_proof") if isinstance(entry, Mapping) else None
        return (
            isinstance(entry, Mapping)
            and entry.get("category") == "gpu-inference-smoke"
            and self._decimal(entry.get("actual")) == Decimal("0.002")
            and entry.get("machine_id") == 31550
            and entry.get("template_hash") == PRIMARY_TEMPLATE_HASH
            and entry.get("image_contract") == PRIMARY_TEMPLATE_IMAGE
            and isinstance(proof, Mapping)
            and proof.get("reads") == 3
        )

    def _has_pinned_reservation_rejection_retry_entitlement(self, reservations: Mapping[str, object]) -> bool:
        entry = reservations.get(INFERENCE_RESERVATION_REJECTION_RUN_ID)
        if (
            not isinstance(entry, Mapping)
            or entry.get("category") != "gpu-inference-smoke"
            or self._decimal(entry.get("amount")) != Decimal("1.00")
            or entry.get("actual") is not None
            or entry.get("absence_proof") is not None
            or entry.get("machine_id") != 28666
            or entry.get("template_hash") != PRIMARY_TEMPLATE_HASH
            or entry.get("image_contract") != PRIMARY_TEMPLATE_IMAGE
        ):
            return False
        run_path = self.path.parent / INFERENCE_RESERVATION_REJECTION_RUN_ID
        journal_path = run_path / "journal" / "journal.ndjson"
        try:
            journal_raw = journal_path.read_bytes()
            if hashlib.sha256(journal_raw).hexdigest() != INFERENCE_RESERVATION_REJECTION_JOURNAL_SHA256:
                return False
            journal = RunJournal.open(journal_path.parent)
            evidence_raw = INFERENCE_RESERVATION_REJECTION_EVIDENCE_PATH.read_bytes()
            if hashlib.sha256(evidence_raw).hexdigest() != INFERENCE_RESERVATION_REJECTION_EVIDENCE_SHA256:
                return False
            evidence = json.loads(evidence_raw)
            request_paths = list((run_path / "desktop-report").glob(".external-report-*/01-preflight-authenticated-session.request.json"))
            response_paths = list((run_path / "desktop-report").glob(".external-report-*/01-preflight-authenticated-session.request.response.json"))
            if len(request_paths) != 1 or len(response_paths) != 1:
                return False
            request_raw = request_paths[0].read_bytes()
            response_raw = response_paths[0].read_bytes()
            request = json.loads(request_raw)
            response = json.loads(response_raw)
            pre_anchor_sums = (run_path / "pre-anchor" / "SHA256SUMS").read_bytes()
            pre_anchor_root = (run_path / "pre-anchor" / "ROOT-HASH.txt").read_text(encoding="utf-8").strip()
            pre_anchor_manifest = (run_path / "pre-anchor" / "run-manifest.json").read_bytes()
            pre_anchor_limitation = (run_path / "pre-anchor" / "limitation.json").read_bytes()
        except (OSError, UnicodeError, InvalidJournal, json.JSONDecodeError, TypeError):
            return False
        events = journal.events()
        terminal = [event for event in events if event.event_type == "terminal.safe"]
        offer = [event for event in events if event.event_type == "offer.pinned"]
        reads = evidence.get("provider_absence_reads") if isinstance(evidence, Mapping) else None
        expected = "frozen offer plus billing and teardown buffer can exceed the stage reservation"
        return (
            [event.event_type for event in events]
            == ["gates.passed", "budget.reserved", "offer.pinned", "report.adapter_ready", "guard.armed", "terminal.safe"]
            and all(event.run_id == INFERENCE_RESERVATION_REJECTION_RUN_ID for event in events)
            and journal.state().value == "TERMINAL"
            and not any(event.event_type in {"provider.create_intent", "provider.create_observed"} for event in events)
            and len(terminal) == 1
            and terminal[0].payload.get("limitation") == expected
            and terminal[0].payload.get("status") == "FAILED_SAFE"
            and len(offer) == 1
            and offer[0].payload.get("offer_id") == 48702994
            and offer[0].payload.get("machine_id") == 28666
            and offer[0].payload.get("dph_total") == "1.3694444444444445"
            and evidence.get("schema") == "srecon26-inference-reservation-rejection-retry-entitlement/v1"
            and evidence.get("run_id") == INFERENCE_RESERVATION_REJECTION_RUN_ID
            and evidence.get("label") == "srecon26-inference--nonce-d27b6fd478c47bf2004c58fa"
            and evidence.get("nonce") == "d27b6fd478c47bf2004c58fa"
            and evidence.get("journal_sha256") == INFERENCE_RESERVATION_REJECTION_JOURNAL_SHA256
            and evidence.get("terminal_limitation") == expected
            and evidence.get("provider_create_calls") == 0
            and evidence.get("guard_anchor_acknowledged") is False
            and evidence.get("authenticated_session_request_sha256") == hashlib.sha256(request_raw).hexdigest()
            and evidence.get("authenticated_session_response_sha256") == hashlib.sha256(response_raw).hexdigest()
            and request.get("schema") == "srecon26-external-report-request/v1"
            and request.get("operation") == "preflight-authenticated-session"
            and request.get("run_id") == INFERENCE_RESERVATION_REJECTION_RUN_ID
            and request.get("nonce") == evidence.get("nonce")
            and response.get("schema") == "srecon26-external-report-response/v1"
            and response.get("authenticated") is True
            and all(response.get(key) == request.get(key) for key in ("operation", "request_id", "run_id", "nonce"))
            and evidence.get("pre_anchor_root") == pre_anchor_root
            and hashlib.sha256(pre_anchor_sums).hexdigest() == pre_anchor_root
            and evidence.get("pre_anchor_manifest_sha256") == hashlib.sha256(pre_anchor_manifest).hexdigest()
            and evidence.get("pre_anchor_limitation_sha256") == hashlib.sha256(pre_anchor_limitation).hexdigest()
            and evidence.get("provider_command") == ["show", "instances", "--raw"]
            and isinstance(reads, list)
            and len(reads) == 3
            and all(
                isinstance(read, Mapping)
                and read.get("matching_instances") == 0
                and read.get("raw") == []
                and isinstance(read.get("observed_at"), str)
                and str(read.get("observed_at")).endswith("Z")
                for read in reads
            )
        )

    def reserve(self, run_id: str, amount: Decimal, category: str, *, machine_id: int | None = None, template_hash: str | None = None, image_contract: str | None = None) -> Decimal:
        amount = self._decimal(amount)
        if amount <= 0 or category not in CATEGORY_CAPS:
            raise BudgetExceeded("invalid budget reservation")
        with self._lock():
            data = self._read()
            reservations = data["reservations"]
            assert isinstance(reservations, dict)
            if machine_id is not None and (
                category != "gpu-inference-smoke"
                or isinstance(machine_id, bool)
                or not isinstance(machine_id, int)
                or machine_id <= 0
            ):
                raise BudgetExceeded("machine binding is restricted to a valid inference machine")
            if run_id in reservations:
                existing = reservations[run_id]
                if (
                    isinstance(existing, Mapping)
                    and self._decimal(existing["amount"]) == amount
                    and existing["category"] == category
                    and existing.get("machine_id") == machine_id
                    and existing.get("template_hash") == template_hash
                    and existing.get("image_contract") == image_contract
                ):
                    return MAX_EXPOSURE - self._exposure(data)
                raise BudgetExceeded("run already has a different reservation")
            if category == "gpu-smoke-retry":
                retries = [entry for entry in reservations.values() if isinstance(entry, Mapping) and entry.get("category") == category]
                pending_smokes = [entry for entry in reservations.values() if isinstance(entry, Mapping) and entry.get("category") == "gpu-smoke" and entry.get("actual") is None and entry.get("absence_proof") is None]
                if retries or len(pending_smokes) != 1:
                    raise BudgetExceeded("smoke retry requires exactly one pending original invoice and one unused retry entitlement")
            if category == "gpu-smoke-distinct-machine":
                recoveries = [entry for entry in reservations.values() if isinstance(entry, Mapping) and entry.get("category") == category]
                pending_smokes = [entry for entry in reservations.values() if isinstance(entry, Mapping) and entry.get("category") == "gpu-smoke" and entry.get("actual") is None and entry.get("absence_proof") is None]
                settled_retries = [entry for entry in reservations.values() if isinstance(entry, Mapping) and entry.get("category") == "gpu-smoke-retry" and entry.get("actual") is not None and entry.get("absence_proof") is not None]
                if recoveries or len(pending_smokes) != 1 or len(settled_retries) != 1:
                    raise BudgetExceeded("distinct-machine smoke requires one pending original, one settled retry, and one unused entitlement")
            if category == "gpu-inference-smoke":
                inference_smokes = [entry for entry in reservations.values() if isinstance(entry, Mapping) and entry.get("category") == category]
                if len(inference_smokes) >= 12:
                    raise BudgetExceeded("direct inference smoke allows at most twelve reservations")
                if len(inference_smokes) == 4 and not self._has_pinned_inference_replacement_entitlement(reservations):
                    raise BudgetExceeded("fifth inference reservation requires pinned no-create replacement evidence")
                if len(inference_smokes) == 5 and not self._has_pinned_measurement_retry_entitlement(reservations):
                    raise BudgetExceeded("sixth inference reservation requires pinned measurement-retry evidence")
                if len(inference_smokes) == 6 and not self._has_pinned_startup_retry_entitlement(reservations):
                    raise BudgetExceeded("seventh inference reservation requires pinned provider-startup retry evidence")
                if len(inference_smokes) == 7 and not self._has_alternate_template_authorization(template_hash, image_contract):
                    raise BudgetExceeded("eighth inference reservation requires pinned alternate-template authorization")
                if len(inference_smokes) == 8 and (
                    not self._has_pinned_report_session_retry_entitlement(reservations)
                    or not self._has_alternate_template_authorization(template_hash, image_contract)
                ):
                    raise BudgetExceeded("ninth inference reservation requires pinned no-create report-session evidence and alternate-template authorization")
                if len(inference_smokes) == 9 and (
                    not self._has_settled_alternate_template_failure(reservations)
                    or template_hash != PRIMARY_TEMPLATE_HASH
                    or image_contract != PRIMARY_TEMPLATE_IMAGE
                ):
                    raise BudgetExceeded("tenth inference reservation requires settled alternate-template failure and exact primary-template recovery")
                if len(inference_smokes) == 10 and (
                    not self._has_settled_terminal_startup_failure(reservations)
                    or template_hash != PRIMARY_TEMPLATE_HASH
                    or image_contract != PRIMARY_TEMPLATE_IMAGE
                ):
                    raise BudgetExceeded("eleventh inference reservation requires settled terminal-startup failure and exact primary-template recovery")
                if len(inference_smokes) == 11 and (
                    not self._has_pinned_reservation_rejection_retry_entitlement(reservations)
                    or template_hash != PRIMARY_TEMPLATE_HASH
                    or image_contract != PRIMARY_TEMPLATE_IMAGE
                ):
                    raise BudgetExceeded("twelfth inference reservation requires pinned no-create reservation-rejection evidence and exact primary-template recovery")
                if amount > Decimal("1.00"):
                    raise BudgetExceeded("direct inference smoke reservation must be no greater than 1.00")
                if machine_id is not None and any(entry.get("machine_id") == machine_id for entry in inference_smokes):
                    raise BudgetExceeded("direct inference smoke machine is already reserved by another attempt")
            category_total = self._category_exposure(data, category)
            if category_total + amount > CATEGORY_CAPS[category] or self._exposure(data) + amount > MAX_EXPOSURE:
                raise BudgetExceeded("reservation would exceed approved exposure")
            entry: dict[str, object] = {"amount": str(amount), "category": category, "actual": None, "absence_proof": None}
            if machine_id is not None:
                entry["machine_id"] = machine_id
            if template_hash is not None:
                entry["template_hash"] = template_hash
            if image_contract is not None:
                entry["image_contract"] = image_contract
            reservations[run_id] = entry
            self._write(data)
            return MAX_EXPOSURE - self._exposure(data)

    def headroom(self) -> Decimal:
        with self._lock():
            return MAX_EXPOSURE - self._exposure(self._read())

    def smoke_run_ids(self) -> tuple[str, ...]:
        """Return validated paid-smoke reservation IDs for history binding."""

        with self._lock():
            data = self._read()
            reservations = data["reservations"]
            assert isinstance(reservations, Mapping)
            return tuple(
                sorted(
                    run_id
                    for run_id, entry in reservations.items()
                    if isinstance(run_id, str)
                    and isinstance(entry, Mapping)
                    and str(entry.get("category", "")).startswith("gpu-smoke")
                )
            )

    def run_ids_for_category(self, category: str) -> tuple[str, ...]:
        """Return validated reservation IDs for one exact approved category."""

        if category not in CATEGORY_CAPS:
            raise ValueError("unknown budget category")
        with self._lock():
            data = self._read()
            reservations = data["reservations"]
            assert isinstance(reservations, Mapping)
            return tuple(
                sorted(
                    run_id
                    for run_id, entry in reservations.items()
                    if isinstance(run_id, str)
                    and isinstance(entry, Mapping)
                    and entry.get("category") == category
                )
            )

    def commit_actual(self, run_id: str, actual: Decimal, absence_proof: Mapping[str, object] | None) -> None:
        if not absence_proof or absence_proof.get("reads") != 3:
            raise ValueError("three-read absence proof is required before actual spend is committed")
        with self._lock():
            data = self._read()
            reservations = data["reservations"]
            assert isinstance(reservations, dict)
            if run_id not in reservations:
                raise BudgetExceeded("unknown reservation")
            entry = reservations[run_id]
            assert isinstance(entry, dict)
            actual = self._decimal(actual)
            if actual < 0 or actual > self._decimal(entry["amount"]):
                raise BudgetExceeded("actual spend must fit within the reservation")
            candidate = dict(entry)
            candidate["actual"] = str(actual)
            candidate["absence_proof"] = dict(absence_proof)
            self._validate_entry(run_id, candidate)
            entry["actual"] = str(actual)
            entry["absence_proof"] = dict(absence_proof)
            self._write(data)
