# Provider fault reporting

The report fixture is the only report path enabled in this repository. It is an offline `file://` Playwright page with a restrictive CSP (`connect-src 'none'`) and no provider credential, API call, or external navigation.

Run it with:

```bash
python3 scripts/preflight_report.py --fixture --json artifacts/report-fixture-attestation.json
```

The output receipt records the exact fixture instance ID, run label, nonce, and `provider_request_count: 0`, plus redacted before/after screenshots. A mismatched ID, label, or nonce refuses the click and writes no receipt.

Real reporting is intentionally unavailable in the fixture driver. A future live adapter must require a frozen `PROVIDER_FAULT_CONFIRMED` record, an exact ID/label/nonce match, a genuine HTTPS provider URL, lifecycle state `REPORTING_FAULT`, the bounded report window, and explicit live-run approval. It must capture redacted screenshots and never delay the independent teardown deadline.
