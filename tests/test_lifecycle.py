from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Event
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from portfolio_backend.db import Database
from portfolio_backend.models import Consent, Device, Job, Preferences, Report
from portfolio_backend.providers.ai import AIAnswer
from portfolio_backend.scheduler import tick
from portfolio_backend.services import jobs
from portfolio_backend.worker import execute

M = "/api/mobile/v1"


def test_role_misconfiguration_fails_closed(cfg):
    db = Database(cfg.model_copy(update={"database_url": cfg.control_database_url}))
    try:
        with pytest.raises(RuntimeError):
            db.verify_roles()
    finally:
        db.close()


def test_scheduler_deduplicates_and_respects_empty_portfolios(db, user, instrument, opening):
    a, _empty = user(), user()
    opening(a, instrument())
    with db.control() as s:
        for pref in s.scalars(select(Preferences)):
            pref.daily_report = pref.weekly_report = True
    # Saturday 2026-10-24 CN: US session (Fri 16:30 ET) opens early morning,
    # the weekly starts 09:00 CN and the A/HK daily at 16:30 CN.
    early = datetime(2026, 10, 24, 2, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert tick(db, early) == 0  # US Friday window not reached yet
    morning = datetime(2026, 10, 24, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert tick(db, morning) == 2  # US Friday recap + weekly
    assert tick(db, morning) == 0  # deduplicated
    cn_close = datetime(2026, 10, 24, 16, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert tick(db, cn_close) == 1  # A/HK daily session
    assert tick(db, cn_close) == 0  # deduplicated
    jobs.materialize_outbox(db)
    with db.control() as s:
        assert set(s.scalars(select(Job.user_id))) == {a["id"]}


def test_report_followups_use_free_quota_and_sse_is_owned(client, db, cfg, user):
    a, b = user(), user()
    with db.control() as s:
        report = Report(
            user_id=a["id"],
            kind="daily",
            period="test",
            title="复盘",
            summary="摘要",
            snapshot={"positions": []},
        )
        s.add(report)
        s.add(
            Consent(
                user_id=a["id"],
                purpose="interactive_ai",
                provider="test-provider",
                disclosure_version="1",
                granted=True,
            )
        )
        s.flush()
        rid = report.id

    class Gateway:
        def answer(self, payload):
            return AIAnswer(text="回答", model="test-model")

    for i in range(3):
        response = client.post(
            f"{M}/reports/{rid}/questions",
            json={"content": "解释一下"},
            headers=a["headers"] | {"Idempotency-Key": str(i)},
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["data"]["run_id"]
        jobs.materialize_outbox(db)
        with db.control() as s:
            jid = s.scalar(select(Job.id).where(Job.status == "queued"))
        assert execute(db, jid, cfg, Gateway()) == "succeeded"
    denied = client.post(
        f"{M}/reports/{rid}/questions",
        json={"content": "第四次"},
        headers=a["headers"] | {"Idempotency-Key": "fourth"},
    )
    assert denied.status_code == 403
    assert client.get(f"{M}/runs/{run_id}/events", headers=b["headers"]).status_code == 404
    response = client.get(f"{M}/runs/{run_id}/events", headers=a["headers"])
    assert response.status_code == 200 and '"status": "succeeded"' in response.text
    with db.control() as s:
        row = s.get(Report, rid)
        assert row.free_used == 3 and row.free_reserved == 0


def test_delete_during_provider_call_rejects_late_result(client, db, cfg, user):
    a = user()
    with db.control() as s:
        report = Report(
            user_id=a["id"],
            kind="daily",
            period="test",
            title="复盘",
            summary="摘要",
            snapshot={"positions": []},
        )
        s.add(report)
        s.add(
            Consent(
                user_id=a["id"],
                purpose="interactive_ai",
                provider="test-provider",
                disclosure_version="1",
                granted=True,
            )
        )
        s.flush()
        rid = report.id
    response = client.post(
        f"{M}/reports/{rid}/questions",
        json={"content": "解释"},
        headers=a["headers"] | {"Idempotency-Key": "deletion-race"},
    )
    assert response.status_code == 202
    jobs.materialize_outbox(db)
    with db.control() as s:
        answer_job = s.scalar(select(Job.id).where(Job.kind == "answer"))
    started, release = Event(), Event()

    class Gateway:
        def answer(self, payload):
            started.set()
            assert release.wait(10)
            return AIAnswer(text="迟到的回答", model="test-model")

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execute, db, answer_job, cfg, Gateway())
        assert started.wait(5)
        try:
            deletion = client.post(
                M + "/me/deletion-requests", headers=a["headers"], json={"confirmation": "DELETE"}
            )
            assert deletion.status_code == 202
            jobs.materialize_outbox(db)
            with db.control() as s:
                delete_job = s.scalar(select(Job.id).where(Job.kind == "delete_account"))
            assert execute(db, delete_job, cfg) == "succeeded"
        finally:
            release.set()
        assert future.result(timeout=5) in {"stale", "cancelled"}
    with db.control() as s:
        assert s.scalar(select(func.count()).select_from(Report)) == 0


def test_device_rebind_does_not_leak_old_user_data(client, db, user):
    a, b = user(), user()
    token = "a" * 64
    first = client.put(
        f"{M}/me/devices/{uuid4()}",
        headers=a["headers"],
        json={"token": token, "environment": "sandbox"},
    )
    assert first.status_code == 200
    second = client.put(
        f"{M}/me/devices/{uuid4()}",
        headers=b["headers"],
        json={"token": token, "environment": "sandbox"},
    )
    assert second.status_code == 200
    with db.tenant(a["id"]) as s:
        assert s.scalars(select(Device)).all() == []
    assert (
        client.delete(
            f"{M}/me/devices/{second.json()['data']['id']}", headers=a["headers"]
        ).status_code
        == 404
    )
