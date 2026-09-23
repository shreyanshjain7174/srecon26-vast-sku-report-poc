# GitHub-hosted independent guard channel

This PoC workflow is a bounded, disposable control channel for the existing
nonce-bound Vast guard worker. It runs on a standard GitHub-hosted Ubuntu runner
for at most 60 minutes and must be dispatched **before** any paid create. It is
not a replacement for a longer-lived independently reachable teardown service.

## Deployment inputs

Create a **private repository issue** first, then create one encrypted Actions
secret named `VAST_API_KEY`. Do not put the credential in workflow inputs,
comments, logs, artifacts, or local files. Dispatch `Independent Vast deadline
guard` with:

- `nonce`: a new 8-128 character URL-safe nonce;
- `label`: the exact safe provider label that embeds/binds that nonce;
- `issue_number`: the private control issue number;
- `hard_deadline`: a fixed UTC ISO-8601 value no more than 60 minutes ahead;
- `trusted_author`: the GitHub login of the repository owner who will comment;
- `heartbeat_seconds`: 30-600 seconds (120 is the default).

The workflow accepts comments only when all conditions hold: the issue is in the
private repository, the author login equals `trusted_author`, GitHub reports the
author association as `OWNER`, the timestamp is after arming, and the entire
body exactly matches one of these forms (with no Markdown or extra text):

```
SRECON26_GUARD_V1 HEARTBEAT nonce=<nonce> root=<64 lowercase hex>
SRECON26_GUARD_V1 ANCHOR nonce=<nonce> root=<64 lowercase hex>
```

The deadline, nonce, label, owner, issue, and heartbeat window are persisted at
arming and cannot be changed by a later sync or comment. A missed heartbeat or
the immutable deadline causes the guard worker to reconcile the exact
label-and-nonce target and request teardown. If no target exists yet it keeps
watching; this is why the workflow must be armed before a create request.

The worker reads the encrypted Actions secret only after GitHub injects it into a
root-only `0600` file. The channel never receives the secret, and neither the
workflow nor the Python wrapper prints it. The job has only `contents: read`
and `issues: write` permissions; it has no pull-request, deployment, cloud, or
repository-write permission.

At completion (including a failure), download the
`independent-guard-<nonce>` artifact. It contains the root-owned worker
hash-chained journal and the channel journal/state. Review a terminal receipt
and provider-side absence before accepting teardown as complete. Do not reuse a
nonce, issue, or artifact for a later paid run.

## Credential-free Phase 1 anchoring

`Phase 1 anchor-only guard journal` is a separate manual workflow for recording
the selected CPU, queue, and KV Phase 1 SHA-256 roots. It accepts three strict
64-character lowercase hexadecimal roots and a new nonce, derives the label
from the GitHub run identity and nonce, and records all three through the real
guard worker's hash-chained journal. It takes no provider secret, creates no
provider target, and never ticks or claims a live teardown. Download its
`phase1-anchor-<run-id>` artifact for the receipt and guard journal.
