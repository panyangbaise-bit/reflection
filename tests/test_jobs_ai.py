from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from portfolio_backend.db import now
from portfolio_backend.errors import DomainError
from portfolio_backend.models import AIRun, Consent, Job, Outbox, Report
from portfolio_backend.providers.ai import AIAnswer
from portfolio_backend.services import jobs
from portfolio_backend.worker import execute, terminal_failure

M = "/api/mobile/v1"


class Gateway:
    calls = 0

    def answer(self, payload):
        self.calls += 1
        assert "portfolio_id" not in payload["context"]
        assert all("account_id" not in p for p in payload["context"]["positions"])
        return AIAnswer(
            text="持仓集中度来自服务端快照。",
            model="test-model",
            input_tokens=100,
            output_tokens=20,
        )


def report_fixture(db, u):
    with db.control() as s:
        row = Report(
            user_id=u["id"],
            kind="daily",
            period="2026-10-24",
            title="测试复盘",
            summary="测试",
            snapshot={"positions": [], "complete": True},
        )
        s.add(row)
        s.add(
            Consent(
                user_id=u["id"],
                purpose="interactive_ai",
                provider="test-provider",
                disclosure_version="1",
                granted=True,
            )
        )
        s.flush()
        return row.id


def run_queued(db, cfg, gateway):
    """Drain the queue until no immediately-runnable job is left.

    Reports are two-phase now (facts batch, then AI summary batch), so one
    materialize+execute pass can enqueue a follow-up job that must also run.
    """
    results = []
    for _ in range(8):
        jobs.materialize_outbox(db)
        with db.control() as s:
            ids = s.scalars(select(Job.id).where(Job.status == "queued")).all()
        if not ids:
            break
        round_results = [execute(db, jid, cfg, gateway) for jid in ids]
        progressed = [result for result in round_results if result != "ignored"]
        results.extend(progressed)
        if not progressed:
            break
    return results


def all_succeeded(results):
    return bool(results) and set(results) == {"succeeded"}


def test_three_free_questions_and_duplicate_delivery(client, db, cfg, user):
    u = user()
    rid = report_fixture(db, u)
    gateway = Gateway()
    for index in range(3):
        response = client.post(
            f"{M}/reports/{rid}/questions",
            json={"content": "分析集中度"},
            headers=u["headers"] | {"Idempotency-Key": str(index)},
        )
        assert response.status_code == 202, response.text
        assert all_succeeded(run_queued(db, cfg, gateway))
    denied = client.post(
        f"{M}/reports/{rid}/questions",
        json={"content": "第四次"},
        headers=u["headers"] | {"Idempotency-Key": "fourth"},
    )
    assert denied.status_code == 403
    assert gateway.calls == 3
    with db.control() as s:
        row = s.get(Report, rid)
        assert row.free_used == 3 and row.free_reserved == 0
        ids = s.scalars(select(Job.id)).all()
    assert all(execute(db, jid, cfg, gateway) == "ignored" for jid in ids)
    assert gateway.calls == 3


def test_last_free_question_is_reserved_atomically(client, db, cfg, user):
    u = user()
    rid = report_fixture(db, u)
    with db.control() as s:
        s.get(Report, rid).free_used = 2

    def ask(_):
        return client.post(
            f"{M}/reports/{rid}/questions",
            json={"content": "问题"},
            headers=u["headers"] | {"Idempotency-Key": str(uuid4())},
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(ask, range(2))) == [202, 403]
    with db.control() as s:
        assert s.get(Report, rid).free_reserved == 1


def test_consent_revoked_after_enqueue_prevents_external_call(client, db, cfg, user):
    u = user()
    rid = report_fixture(db, u)
    res = client.post(
        f"{M}/reports/{rid}/questions",
        json={"content": "问题"},
        headers=u["headers"] | {"Idempotency-Key": "consent-test"},
    )
    assert res.status_code == 202
    client.put(
        M + "/me/consents/interactive_ai",
        headers=u["headers"],
        json={"granted": False, "provider": "test-provider", "disclosure_version": "1"},
    )
    gateway = Gateway()
    assert run_queued(db, cfg, gateway) == ["failed"]
    assert gateway.calls == 0
    with db.control() as s:
        assert s.get(Report, rid).free_reserved == 0
        assert s.get(Report, rid).free_used == 0


def test_exhausted_provider_failure_releases_quota(client, db, cfg, user):
    class BrokenGateway:
        def answer(self, payload):
            raise DomainError("ai_provider_failed", "unavailable", 503)

    u = user()
    rid = report_fixture(db, u)
    res = client.post(
        f"{M}/reports/{rid}/questions",
        json={"content": "问题"},
        headers=u["headers"] | {"Idempotency-Key": "retry"},
    )
    assert res.status_code == 202
    for _ in range(cfg.max_job_attempts):
        with db.control() as s:
            for job in s.scalars(select(Job)):
                job.due_at = now()
        run_queued(db, cfg, BrokenGateway())
    with db.control() as s:
        assert s.get(Report, rid).free_reserved == 0
        assert s.get(Report, rid).free_used == 0
        assert s.scalar(select(AIRun)).status == "failed"


def test_fencing_rejects_late_worker_and_dispatch_recovers(db, cfg, user):
    u = user()
    with db.control() as s:
        row = Outbox(
            user_id=u["id"], kind="daily", dedupe_key="report-1", payload={"period": "2026-10-24"}
        )
        s.add(row)
    sent = []
    assert jobs.dispatch(db, lambda *args: sent.append(args)) == 1
    jid = UUID(sent[0][0])
    first = jobs.claim(db, jid, cfg)
    with db.control() as s:
        s.get(Job, jid).lease_until = now() - timedelta(seconds=1)
    assert jobs.recover(db, cfg, terminal_failure) == 1
    with db.control() as s:
        s.get(Job, jid).due_at = now()
    second = jobs.claim(db, jid, cfg)
    assert first["fence"] != second["fence"]
    with pytest.raises(DomainError), db.control() as s:
        jobs.locked_job(s, first)
    with db.control() as s:
        assert jobs.locked_job(s, second).attempt == 2


def test_report_once_visible_and_tenant_protected(client, db, cfg, user):
    a, b = user(), user()
    with db.control() as s:
        s.add(
            Outbox(
                user_id=a["id"],
                kind="daily",
                dedupe_key="report-one",
                payload={"period": "2026-10-24"},
            )
        )
    assert all_succeeded(run_queued(db, cfg, Gateway()))
    jobs.materialize_outbox(db)
    with db.control() as s:
        assert s.scalar(select(func.count()).select_from(Report)) == 1
        report = s.scalar(select(Report))
        rid = report.id
    assert client.get(f"{M}/reports/{rid}", headers=b["headers"]).status_code == 404
    assert client.get(M + "/home", headers=a["headers"]).json()["data"]["latest_content"][
        "id"
    ] == str(rid)


def test_scheduled_ai_has_separate_consent_and_revocation(db, cfg, user):
    u = user()
    # Interactive approval alone must not send the scheduled snapshot.
    report_fixture(db, u)
    with db.control() as s:
        s.add(
            Outbox(
                user_id=u["id"],
                kind="daily",
                dedupe_key="separate",
                payload={"period": "2026-10-25"},
            )
        )
    gateway = Gateway()
    assert all_succeeded(run_queued(db, cfg, gateway))
    assert gateway.calls == 0
    with db.control() as s:
        s.add(
            Consent(
                user_id=u["id"],
                purpose="scheduled_ai",
                provider="test-provider",
                disclosure_version="1",
                granted=True,
            )
        )
        s.add(
            Outbox(
                user_id=u["id"],
                kind="daily",
                dedupe_key="authorized",
                payload={"period": "2026-10-26"},
            )
        )
    assert all_succeeded(run_queued(db, cfg, gateway))
    assert gateway.calls == 1
    with db.control() as s:
        report = s.scalar(select(Report).where(Report.period == "2026-10-26"))
        assert report.snapshot["analysis"]["kind"] == "ai"
        assert report.summary == "持仓集中度来自服务端快照。"
        s.add(
            Outbox(
                user_id=u["id"],
                kind="weekly",
                dedupe_key="revoked",
                payload={"period": "2026-10-27"},
            )
        )

    class Revoke(Gateway):
        def answer(self, payload):
            with db.control() as s:
                consent = s.scalar(
                    select(Consent).where(
                        Consent.user_id == u["id"], Consent.purpose == "scheduled_ai"
                    )
                )
                consent.granted = False
            return super().answer(payload)

    assert all_succeeded(run_queued(db, cfg, Revoke()))
    with db.control() as s:
        report = s.scalar(select(Report).where(Report.period == "2026-10-27"))
        assert report.snapshot["analysis"] == {"kind": "facts", "status": "consent_revoked"}
        assert "尚无可核实的期间收益" in report.summary


def test_scheduled_ai_provider_failure_delivers_fact_report(db, cfg, user):
    u = user()
    with db.control() as s:
        s.add(
            Consent(
                user_id=u["id"],
                purpose="scheduled_ai",
                provider="test-provider",
                disclosure_version="1",
                granted=True,
            )
        )
        s.add(
            Outbox(
                user_id=u["id"],
                kind="daily",
                dedupe_key="fallback",
                payload={"period": "2026-10-24"},
            )
        )

    class Failed(Gateway):
        def answer(self, payload):
            raise DomainError("ai_balance_exhausted", "unavailable", 503)

    assert all_succeeded(run_queued(db, cfg, Failed()))
    with db.control() as s:
        r = s.scalar(select(Report))
        assert r.snapshot["analysis"] == {"kind": "facts", "status": "ai_balance_exhausted"}
        assert "尚无可核实" in r.summary
