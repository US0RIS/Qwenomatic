# Safety verification receipt — 2026-10-02 UTC

The committed kernel acceptance test was run without mocks in a separate
QEMU 8.2.2 Linux VM using Ubuntu kernel 6.8.0-138-generic. The VM had no external
network interfaces. Its internal veth/namespace topology and controlled TCP
canary provided the actual positive and negative connection tests.

Command inside the VM:

    /usr/bin/python3.12 -I /opt/qwenomatic/deploy/kernel_smoke.py --python /usr/bin/python3.12

Result: QWENOMATIC_KERNEL_SMOKE_EXIT=0.

The test exercised the real launcher, unprivileged production Supervisor and
ledger, with the normal barrier checks enabled. It verified:

* A live TCP service was reachable from the same farm namespace when explicitly allowed.
* Removing that allow rule caused an actual blocked outbound connection.
* The socket error queue identified ICMP type 3, code 13 (administratively prohibited);
  a generic EHOSTUNREACH, timeout or refused connection does not count.
* Actual farm initialization and two simulation ticks succeeded under the barrier.
* The ledger contained operator-attributed access approval and kernel proof;
  its hash chain verified.
* Starting the farm outside the protected namespace failed closed.
* A writable access manifest was refused.

The console transcript is in kernel-smoke-2026-10-02.txt. The tested production
source files were compared byte-for-byte against the checkout; their SHA256
hashes are in tested-source-sha256.json.

The ordinary simulation/gateway/adversarial suite was also run locally. These
tests inject a boundary fixture and are separate from the real kernel receipt.
The full suite passed: 169 tests (including 47 safety tests).

GitHub Actions was attempted, but all jobs failed before executing any steps and
no job logs were available through the connector. The VM result above is the
kernel verification evidence; GitHub CI is not claimed to have passed.

No live payment provider, credential, payee or external adapter was enabled.
Payment transport behavior was tested with controlled doubles. Provider-specific
settlement must be verified when an operator enables an integration. Deployment
startup repeats the live network proof on the actual deployment host.

## Follow-up review verification — 2026-10-02 UTC

The updated kernel_smoke.py passed against the exact production source hashes
in followup-source-sha256.json, in the same QEMU/Linux environment with 2 GiB
RAM and a protected venv. See kernel-followup-2026-10-02.txt. It additionally
runs Generation Zero, the evolved/control A/B and the campaign through their
fixed protected launcher operations, rejects a writable planted .pth without
executing it, SIGKILLs a launcher after host mutation and verifies independent
idempotent cleanup of namespace, NAT table, evidence and forwarding state.
The full local suite passed 177 tests. Text approval tests check exact-content
binding and single use. Unit tests still use the explicit boundary fixture.

The launcher is now invoked with /usr/bin/python3 -I -S. It checks the entire
explicit venv before any privileged use, and privileged probes also disable
site loading. The farm starts site loading only after dropping privileges.
An earlier VM attempt with insufficient memory exhausted its initramfs space;
the successful 2 GiB run is the receipt above. No live payment relay or Windows
model connection was enabled or tested. The short experiment generations test
entry-point compatibility, not the scientific efficacy of evolution.

## Phase 1 broker and inference boundaries

[PHASE1.md](PHASE1.md) records the executable controls, operator prerequisites,
residual risks and successful controlled VM receipts. The source inventory is
[phase1-source-sha256.json](phase1-source-sha256.json); final broker and baseline
console receipts and the complete local test result are stored alongside it.
This evidence is separate from GitHub CI and production deployment status.
