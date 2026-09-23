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
    "gpu-inference-smoke": Decimal("4.25"),
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

    def reserve(self, run_id: str, amount: Decimal, category: str, *, machine_id: int | None = None) -> Decimal:
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
                if len(inference_smokes) >= 5:
                    raise BudgetExceeded("direct inference smoke allows at most five reservations including one pinned no-create replacement")
                if len(inference_smokes) == 4 and not self._has_pinned_inference_replacement_entitlement(reservations):
                    raise BudgetExceeded("fifth inference reservation requires pinned no-create replacement evidence")
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
