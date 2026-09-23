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
    "canary": Decimal("1.00"),
    "paired-comparison": Decimal("3.00"),
}


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

    def reserve(self, run_id: str, amount: Decimal, category: str) -> Decimal:
        amount = self._decimal(amount)
        if amount <= 0 or category not in CATEGORY_CAPS:
            raise BudgetExceeded("invalid budget reservation")
        with self._lock():
            data = self._read()
            reservations = data["reservations"]
            assert isinstance(reservations, dict)
            if run_id in reservations:
                existing = reservations[run_id]
                if isinstance(existing, Mapping) and self._decimal(existing["amount"]) == amount and existing["category"] == category:
                    return MAX_EXPOSURE - self._exposure(data)
                raise BudgetExceeded("run already has a different reservation")
            category_total = self._category_exposure(data, category)
            if category_total + amount > CATEGORY_CAPS[category] or self._exposure(data) + amount > MAX_EXPOSURE:
                raise BudgetExceeded("reservation would exceed approved exposure")
            reservations[run_id] = {"amount": str(amount), "category": category, "actual": None, "absence_proof": None}
            self._write(data)
            return MAX_EXPOSURE - self._exposure(data)

    def headroom(self) -> Decimal:
        with self._lock():
            return MAX_EXPOSURE - self._exposure(self._read())

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
