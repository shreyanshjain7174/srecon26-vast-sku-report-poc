from datetime import UTC, datetime, timedelta

from srecon26_poc.contracts import InstanceContract, OfferContract, ProbeOutcome
from srecon26_poc.controller import ExperimentController
from srecon26_poc.guard import GuardAttestation
from srecon26_poc.journal import RunJournal
from srecon26_poc.types import RunIdentity, RunState


NOW = datetime(2026, 9, 23, tzinfo=UTC)

class Guard:
    def preflight(self): return GuardAttestation("guard.example", "script", "nonce", "label", NOW + timedelta(minutes=5), NOW)
    def arm(self, identity, hard_deadline): return self.preflight()
    def record_heartbeat(self, identity, monotonic_ns): pass

class Provider:
    def __init__(self): self.create_calls = 0; self.destroyed = False; self.instance = InstanceContract(1,"RTX",1,1,"1",1,OfferContract(1,"RTX",1,1,"1",1,0,"label").dph_total,"label")
    def create_once(self, contract, request_key): self.create_calls += 1; return self.instance
    def list_instances(self): return () if self.destroyed else (self.instance,)
    def destroy_exact(self, instance_id, expected_label): assert (instance_id, expected_label) == (1,"label"); self.destroyed = True

def armed_journal(tmp_path):
    j = RunJournal.create(tmp_path, RunIdentity("run", "label", NOW))
    for state, event, tick in [(RunState.OFFLINE_VALIDATED,"offline",1),(RunState.BUDGET_RESERVED,"budget",2),(RunState.OFFER_PINNED,"offer",3),(RunState.REPORT_ADAPTER_READY,"report",4),(RunState.GUARD_ARMED,"guard",5)]: j.append(state,event,{},NOW,tick)
    return j

def test_create_intent_is_not_reissued_after_reopen(tmp_path):
    provider = Provider(); journal = armed_journal(tmp_path); controller = ExperimentController(journal, provider, Guard(), None)
    controller.create_or_reconcile(OfferContract(1,"RTX",1,1,"1",1,0,"label"))
    assert provider.create_calls == 1
    reopened = ExperimentController(RunJournal.open(tmp_path), provider, Guard(), None)
    reopened.create_or_reconcile(OfferContract(1,"RTX",1,1,"1",1,0,"label"))
    assert provider.create_calls == 1
