"""One-command isolated local stack; no remote credentials, no production data."""

import argparse
import hashlib
import json
import os
import secrets
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ["docker", "compose", "-f", str(ROOT / "compose.simulator.yaml")]
PRIVATE = ROOT / "secrets" / "simulator"


def run(*args, **kwargs):
    return subprocess.run(list(args), check=True, cwd=ROOT, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "session", "revoke", "status", "reset"])
    parser.add_argument("--erase-local-fixtures", action="store_true")
    parser.add_argument("--simulator", help="Booted simulator UDID for session import")
    args = parser.parse_args()
    PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    token_file = PRIVATE / "admin-token"
    if not token_file.exists():
        fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32))
    credential = hashlib.sha256(token_file.read_text().strip().encode()).hexdigest()
    os.environ["PA_SIM_ADMIN_CREDENTIALS"] = json.dumps(
        {credential: {"name": "local-simulator", "role": "owner"}}
    )
    if args.action == "reset":
        if not args.erase_local_fixtures:
            parser.error(
                "reset requires --erase-local-fixtures; erases ONLY reflection-simulator volumes"
            )
        run(*COMPOSE, "down", "--volumes", "--remove-orphans")
        return
    if args.action == "stop":
        run(*COMPOSE, "down")
        return
    if args.action == "status":
        run(*COMPOSE, "ps")
        return
    if args.action == "revoke":
        run(
            *COMPOSE,
            "exec",
            "-T",
            "api",
            "python",
            "-c",
            "from portfolio_backend.config import settings; from portfolio_backend.db import Database; from portfolio_backend.models import AuthSession, User; from sqlalchemy import select, update; db=Database(settings());\nwith db.control() as s: s.execute(update(AuthSession).where(AuthSession.user_id.in_(select(User.id).where(User.apple_subject=='local-simulator-fixture'))).values(revoked=True))",
        )
        return
    if args.action == "start":
        run(*COMPOSE, "build", "api")
        run(*COMPOSE, "up", "-d", "--wait", "postgres", "cache", "broker")
        run(*COMPOSE, "run", "--rm", "migrate")
        run(*COMPOSE, "up", "-d", "--wait", "api", "worker", "dispatcher", "scheduler")
    # exec in API container preserves private file without a dangling seed container.
    run(*COMPOSE, "exec", "-T", "api", "python", "scripts/seed-simulator.py")
    container = run(*COMPOSE, "ps", "-q", "api", capture_output=True, text=True).stdout.strip()
    session = PRIVATE / "session.json"
    run("docker", "cp", container + ":/tmp/simulator-session.json", str(session))
    session.chmod(0o600)
    run(*COMPOSE, "exec", "-T", "api", "rm", "-f", "/tmp/simulator-session.json")
    if args.simulator:
        container_path = run(
            "xcrun",
            "simctl",
            "get_app_container",
            args.simulator,
            "com.portfolioagent.ios",
            "data",
            capture_output=True,
            text=True,
        ).stdout.strip()
        target = Path(container_path) / "Library" / "Application Support" / "beta-session.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(session.read_bytes())
        target.chmod(0o600)
        subprocess.run(
            ["xcrun", "simctl", "terminate", args.simulator, "com.portfolioagent.ios"],
            capture_output=True,
        )
        run(
            "xcrun",
            "simctl",
            "launch",
            args.simulator,
            "com.portfolioagent.ios",
            "--beta-cloud-only",
        )
    print(
        "Local Sandbox ready at http://127.0.0.1:8000; credentials in secrets/simulator (not printed)."
    )


if __name__ == "__main__":
    main()
