"""Staging capacity harness. Use synthetic accounts, never personal portfolios.

PA_LOAD_USERS points to a JSON array of {access_token, position_id?}.
One distinct synthetic identity per simulated client prevents cross-device write noise.
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty, Queue
from uuid import uuid4

from locust import HttpUser, between, task
from locust.exception import StopUser

users = Queue()
source = os.environ.get("PA_LOAD_USERS")
if source:
    for row in json.loads(Path(source).read_text()):
        users.put(row)


class PortfolioUser(HttpUser):
    wait_time = between(0.2, 1)

    def on_start(self):
        try:
            self.identity = users.get_nowait()
        except Empty:
            raise StopUser() from None
        self.client.headers.update(Authorization="Bearer " + self.identity["access_token"])

    def on_stop(self):
        if hasattr(self, "identity"):
            users.put(self.identity)

    @task(4)
    def snapshot(self):
        self.client.get("/api/mobile/v1/portfolio/snapshot", name="snapshot")

    @task(3)
    def home(self):
        self.client.get("/api/mobile/v1/home", name="home")

    @task(2)
    def history(self):
        self.client.get("/api/mobile/v1/feed?limit=20", name="feed")

    @task(1)
    def ledger(self):
        self.client.get("/api/mobile/v1/ledger-events?limit=50", name="ledger")

    @task(1)
    def write(self):
        position_id = self.identity.get("position_id")
        if not position_id:
            return
        response = self.client.get("/api/mobile/v1/positions", name="positions-for-write")
        if response.status_code != 200:
            return
        position = next((p for p in response.json()["data"] if p["id"] == position_id), None)
        if position:
            self.client.post(
                "/api/mobile/v1/ledger-events",
                name="manual-buy",
                headers={"Idempotency-Key": str(uuid4())},
                json={
                    "position_id": position_id,
                    "kind": "buy",
                    "quantity": "0.001",
                    "price": "4.2",
                    "expected_version": position["version"],
                    "occurred_at": datetime.now(UTC).isoformat(),
                },
            )
