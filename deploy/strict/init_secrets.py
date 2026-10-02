"""Create the supervisor/broker capability-signing secret once.

This is an infrastructure secret, not an external provider credential.
Provider credentials remain operator-supplied files in provider-secrets/.
"""

from __future__ import annotations

import os
from pathlib import Path

path = Path("/run/qwenomatic-control/capability.key")
path.parent.mkdir(parents=True, exist_ok=True)
if not path.exists():
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(os.urandom(32))
os.chmod(path, 0o600)
try:
    os.chown(path, 10001, 10001)
except PermissionError:
    pass
print("capability secret ready")
