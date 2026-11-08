"""Create a named operator credential locally. Never print the raw token."""

import hashlib
import json
import os
import secrets
from pathlib import Path

token = secrets.token_urlsafe(48)
directory = Path("secrets")
directory.mkdir(mode=0o700, exist_ok=True)
path = directory / "admin-token.txt"
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as handle:
    handle.write(token)
credential = {hashlib.sha256(token.encode()).hexdigest(): {"name": "local-owner", "role": "owner"}}
print("Raw credential saved to secrets/admin-token.txt")
print("Set PA_ADMIN_CREDENTIALS to: " + json.dumps(credential))
