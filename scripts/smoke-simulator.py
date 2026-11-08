"""Exercise local HTTP + broker + Worker + trace without external AI or real assets."""

import json
import time
from pathlib import Path
from uuid import uuid4

import httpx

root = Path(__file__).resolve().parents[1]
session = json.loads((root / "secrets/simulator/session.json").read_text())
assert session["origin"] == "http://127.0.0.1:8000"
with httpx.Client(
    base_url=session["origin"],
    headers={"Authorization": "Bearer " + session["tokens"]["accessToken"]},
    timeout=10,
) as c:

    def data(method, path, **kwargs):
        r = c.request(method, "/api/mobile/v1/" + path, **kwargs)
        if r.status_code >= 400:
            raise RuntimeError(
                f"{method} {path}: {r.status_code} {r.json().get('error', {}).get('code')}"
            )
        return r.json()["data"]

    me = data("GET", "me")
    assert me["store_environment"] == "Sandbox"
    conversation = data(
        "POST",
        "conversations",
        json={"title": "本机端到端合成验收"},
        headers={"Idempotency-Key": str(uuid4())},
    )
    headers = {"Idempotency-Key": str(uuid4())}
    path = f"conversations/{conversation['id']}/messages"
    queued = data(
        "POST", path, json={"content": "解释本机合成组合；不调用外部服务。"}, headers=headers
    )
    replay = data(
        "POST", path, json={"content": "解释本机合成组合；不调用外部服务。"}, headers=headers
    )
    assert replay["run_id"] == queued["run_id"]
    for _ in range(30):
        run = data("GET", "runs/" + queued["run_id"])
        if run["status"] in {"succeeded", "failed"}:
            break
        time.sleep(1)
    assert run["status"] == "succeeded", {k: v for k, v in run.items() if k != "answer"}
    assert "本机合成联调回答" in run["answer"]
    messages = data("GET", path + "?paged=true&limit=1")
    assert len(messages["items"]) == 1 and messages["next_cursor"]
    reports = data("GET", "feed?limit=1")
    rid = reports["items"][0]["id"]
    report = data("GET", f"reports/{rid}")
    assert report["snapshot"]["positions"]
    data("POST", f"reports/{rid}/read")
    assert data("GET", f"reports/{rid}")["read"]
    question_headers = {"Idempotency-Key": str(uuid4())}
    question = data(
        "POST",
        f"reports/{rid}/questions",
        json={"content": "解释这份本机合成报告的数据依据。"},
        headers=question_headers,
    )
    duplicate = data(
        "POST",
        f"reports/{rid}/questions",
        json={"content": "解释这份本机合成报告的数据依据。"},
        headers=question_headers,
    )
    assert question["run_id"] == duplicate["run_id"]
    for _ in range(30):
        followup = data("GET", "runs/" + question["run_id"])
        if followup["status"] in {"succeeded", "failed"}:
            break
        time.sleep(1)
    assert followup["status"] == "succeeded"
    for section in ("positions", "ledger_events", "reports", "messages", "agent_tool_calls"):
        page = data("GET", f"me/export?section={section}&limit=1")
        assert page["schema_version"] == 1 and page["items"]
        assert all("user_id" not in item for item in page["items"])
    prefs = data("GET", "me/preferences")
    assert isinstance(prefs["daily_report"], bool) and isinstance(prefs["weekly_report"], bool)
    nav = data("GET", "instruments?query=SIMNAV")["items"][0]
    assert nav["quote_kind"] == "nav" and nav["quote_status"] == "nav"
    assert float(nav["nav_change_pct"]) == 5
    chart = data("GET", "instruments/" + nav["id"] + "/kline")
    assert chart["provider"] == "local-fixture" and len(chart["points"]) == 4
    existing, offset = [], 0
    while True:
        page = data("GET", f"watchlist?limit=100&offset={offset}")
        existing.extend(page["items"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    old = next((item for item in existing if item["instrument_id"] == nav["id"]), None)
    data(
        "PUT",
        "watchlist",
        json={
            "instrument_id": nav["id"],
            "priority": 2,
            "target_price": "1.2",
            "note": "本机合成验收",
        },
    )
    assert any(
        item["instrument_id"] == nav["id"] for item in data("GET", "watchlist?limit=100")["items"]
    )
    if old:
        data(
            "PUT",
            "watchlist",
            json={
                "instrument_id": nav["id"],
                "priority": old["priority"],
                "target_price": old["target_price"],
                "note": old["note"],
            },
        )
    else:
        assert data("DELETE", "watchlist/" + nav["id"])["removed"]
    admin = (root / "secrets/simulator/admin-token").read_text().strip()
    r = c.get(
        "/api/admin/v1/runs/" + queued["run_id"] + "/tool-calls",
        headers={"Authorization": "Bearer " + admin},
    )
    r.raise_for_status()
    trace = r.json()["data"]["items"]
    assert [t["tool_name"] for t in trace] == ["llm", "get_portfolio", "llm"], trace
    assert all(t["status"] == "succeeded" for t in trace)
    summary = {
        "ask": "passed",
        "idempotency": "passed",
        "message_pagination": "passed",
        "report_read": "passed",
        "report_followup": "passed",
        "data_export": "passed",
        "preferences": "passed",
        "nav_change": "passed",
        "watchlist": "passed",
        "chart": "passed",
        "trace": [t["tool_name"] for t in trace],
        "run_id": queued["run_id"],
        "local_only": True,
    }
    print(json.dumps(summary, ensure_ascii=False))
