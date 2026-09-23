# SRECon26 LLM HPA PoC

This repository builds offline, deterministic safety controls and local HPA evidence before any paid provider activity. It does not contain a live provider adapter or credentials.

Run the test suite with `make test`. The test target disables unrelated globally installed pytest plugins so the repository has a deterministic, dependency-free test environment.

Run `make setup-hooks` once per checkout. The tracked hook requires Semgrep and blocks staged provider-secret patterns on the project branch.
