"""Each service must prove its own privileged namespace installation."""
import os
from pathlib import Path
from supervisor.safety.boundary import NetworkBoundary, SafetyError, protected_json


class ServiceBoundary:
    def __init__(self, role):
        if os.geteuid() == 0 or role not in ("broker", "inference"):
            raise SafetyError("service must be unprivileged")
        status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
        if any(int(status[k].strip(), 16) for k in ("CapEff", "CapPrm", "CapInh", "CapAmb", "CapBnd")) or status["NoNewPrivs"].strip() != "1":
            raise SafetyError("service requires zero capabilities/no_new_privs")
        inode = os.stat('/proc/self/ns/net').st_ino
        self.evidence = protected_json(Path(f"/run/qwenomatic-services/{inode}.json"))
        if self.evidence["role"] != role or self.evidence["uid"] != os.geteuid() or self.evidence["namespace"] != os.readlink('/proc/self/ns/net'):
            raise SafetyError("service boundary mismatch")
        # Reuse the exact live kernel rejection logic, not a weaker marker check.
        self.check()

    def check(self):
        proof = object.__new__(NetworkBoundary)
        proof.evidence = self.evidence
        NetworkBoundary.check(proof)
