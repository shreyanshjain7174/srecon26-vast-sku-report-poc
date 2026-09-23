# Independent Azure guard runbook

This path uses an existing, dedicated Azure VM as a durable external Vast
teardown controller. The lifecycle script is read-only: it does not deploy,
modify, delete, request quota, or accept a Vast credential. A paid Vast create
remains forbidden until the controller is separately deployed, reachable, and
returns a nonce-bound arm receipt.

## Azure ownership preflight

Use a dedicated resource group whose group and every resource carry both
`srecon26-purpose=independent-vast-guard` and `srecon26-dedicated=true`. Attach
the named user-assigned identity to the named controller VM. Then run:

```sh
PYTHONPATH=src:. python3 scripts/azure_guard_lifecycle.py preflight \
  --subscription-id "$AZURE_SUBSCRIPTION_ID" \
  --resource-group srecon26-independent-guard-rg \
  --identity-name srecon26-guard-id \
  --controller-vm srecon26-guard-vm \
  --location centralindia \
  --output azure-guard-preflight.json
```

The script performs only `az group show`, `az identity show`, `az vm show`, and
`az resource list`. It verifies the group boundary, the dedicated tags on every
resource, and the exact user-assigned identity on the controller VM. Azure CLI
authentication comes from the operator's existing login; do not add provider
credentials, cloud-init, deployment parameters, or secret values to this
command or its output.

## Pin the host and restrict the runtime key

Obtain the VM's Ed25519 host-key fingerprint from an authenticated Azure
console or the separately verified image/provisioning record. Compare it over
that independent channel before writing the exact host key to a dedicated
known-hosts file. Never approve a first-seen key from the network and never use
`StrictHostKeyChecking=accept-new` or `no`.

The runtime SSH public key must have a single `authorized_keys` entry like:

```text
restrict,command="/usr/local/sbin/guardctl" ssh-ed25519 <runtime-public-key>
```

`guardctl` must reject every `SSH_ORIGINAL_COMMAND` except the literal
`guardctl`, read at most one bounded `srecon26-guard-v1` JSON request from
stdin, dispatch only `preflight`, `arm`, `heartbeat`, `status`, or `anchor`, and
write exactly one JSON object to stdout. It must never start a shell. The
client additionally disables configuration files, PTY allocation, agent and
port forwarding, local commands, password/interactive authentication, and
host-key TOFU.

## Install the provider credential manually

Credential installation is a one-time bootstrap operation, not a runtime RPC.
After the host key has been verified as above, use a separate temporary admin
key and the fixed audited command below; pass the credential only on stdin:

```sh
ssh -F /dev/null -T -o BatchMode=yes -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=/secure/azure-guard-known-hosts \
  -o GlobalKnownHostsFile=/dev/null -o ForwardAgent=no \
  -o ClearAllForwardings=yes -i /secure/bootstrap-admin-key \
  admin@guard.example.test \
  'sudo /usr/local/sbin/guardctl install-credential' \
  < /secure/vast-api-key
```

The audited installer must write `/etc/srecon26-guard/vast-api-key` as a
root-owned `0600` file without echoing stdin. Remove the bootstrap key and its
authorization immediately afterward. Never put the credential in source,
Bicep, cloud-init, shell arguments, environment dumps, logs, or evidence.

## Arm, verify, and collect

Create a fresh 8-128 character nonce and the exact provider label
`<run-label>--nonce-<nonce>`. Send `arm` before any Vast create. Accept the
receipt only when it has all of the following:

- status `ARMED`;
- the exact nonce, label, and immutable UTC hard deadline requested;
- the expected remote host identity and the deployed worker's SHA-256 hash;
- a 64-character lowercase journal root hash.

Call `preflight` after arming so `GuardClient` can construct and validate the
attestation. Send bounded heartbeats during the run. Anchor the local evidence
root, then collect `status` receipts and copy the hash-chained controller
journal through the approved evidence export path. Redact infrastructure
details as needed, but retain timestamps, worker hash, receipt roots, exact
instance ID and label, and the provider observation outcomes. Never include
the credential or private SSH keys.

## Cleanup gate

A destroy request, missing instance response, or one empty inventory read is
not cleanup proof. Keep the Azure VM, identity, resource group, runtime key,
timer, and journal intact through errors and `ABSENCE_PENDING`. Azure cleanup
is allowed only after the durable controller returns `ABSENCE_CONFIRMED` with
three distinct successful post-teardown provider reads showing no match for
both the exact numeric instance ID and exact nonce-bound label. Export and
verify the final journal before manually deleting the dedicated Azure resource
group. If ownership changes, inventory is unknown, or any read fails, retain
the controller and escalate; do not disarm or delete it.
