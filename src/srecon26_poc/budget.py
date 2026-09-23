from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Iterator, Mapping


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
            return data
        except (OSError, ValueError, json.JSONDecodeError) as exc:
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

    @classmethod
    def _reserved(cls, data: Mapping[str, object]) -> Decimal:
        reservations = data["reservations"]
        assert isinstance(reservations, Mapping)
        return sum((cls._decimal(entry["amount"]) for entry in reservations.values() if isinstance(entry, Mapping)), Decimal("0.00"))

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
                    return self.headroom()
                raise BudgetExceeded("run already has a different reservation")
            category_total = sum((self._decimal(entry["amount"]) for entry in reservations.values() if isinstance(entry, Mapping) and entry["category"] == category), Decimal("0.00"))
            if category_total + amount > CATEGORY_CAPS[category] or self._reserved(data) + amount > MAX_EXPOSURE:
                raise BudgetExceeded("reservation would exceed approved exposure")
            reservations[run_id] = {"amount": str(amount), "category": category, "actual": None, "absence_proof": None}
            self._write(data)
            return MAX_EXPOSURE - self._reserved(data)

    def headroom(self) -> Decimal:
        with self._lock():
            return MAX_EXPOSURE - self._reserved(self._read())

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
            entry["actual"] = str(self._decimal(actual))
            entry["absence_proof"] = dict(absence_proof)
            self._write(data)
