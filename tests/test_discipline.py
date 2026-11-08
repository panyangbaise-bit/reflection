from sqlalchemy import select

from portfolio_backend.models import Job, TradingDiscipline
from portfolio_backend.services import jobs
from portfolio_backend.worker import execute

M = "/api/mobile/v1"


def test_discipline_requires_authentication(client):
    assert client.get(M + "/me/discipline").status_code == 401
    assert client.put(M + "/me/discipline", json={"content": "规则"}).status_code == 401


def test_discipline_roundtrip_and_user_isolation(client, user):
    first, second = user(), user()
    empty = client.get(M + "/me/discipline", headers=first["headers"])
    assert empty.status_code == 200, empty.text
    assert empty.json()["data"] == {"content": "", "updated_at": None}

    content = "核心仓的建仓必须分批，首笔不超过计划仓位的 1/3。\n卫星仓先定退出条件。"
    saved = client.put(M + "/me/discipline", headers=first["headers"], json={"content": content})
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["saved"] is True
    assert saved.json()["data"]["updated_at"] is not None

    loaded = client.get(M + "/me/discipline", headers=first["headers"])
    assert loaded.json()["data"]["content"] == content
    assert loaded.json()["data"]["updated_at"] == saved.json()["data"]["updated_at"]

    other = client.get(M + "/me/discipline", headers=second["headers"])
    assert other.json()["data"]["content"] == ""

    updated = client.put(
        M + "/me/discipline", headers=first["headers"], json={"content": "更新后的规则"}
    )
    assert updated.status_code == 200
    assert (
        client.get(M + "/me/discipline", headers=first["headers"]).json()["data"]["content"]
        == "更新后的规则"
    )

    cleared = client.put(M + "/me/discipline", headers=first["headers"], json={"content": ""})
    assert cleared.status_code == 200
    assert (
        client.get(M + "/me/discipline", headers=first["headers"]).json()["data"]["content"] == ""
    )


def test_discipline_rejects_overlong_content(client, user):
    u = user()
    response = client.put(M + "/me/discipline", headers=u["headers"], json={"content": "规" * 2001})
    assert response.status_code == 422
    assert client.get(M + "/me/discipline", headers=u["headers"]).json()["data"]["content"] == ""


def test_account_deletion_removes_discipline(client, db, cfg, user):
    u = user()
    client.put(M + "/me/discipline", headers=u["headers"], json={"content": "删除前规则"})
    response = client.post(
        M + "/me/deletion-requests", headers=u["headers"], json={"confirmation": "DELETE"}
    )
    assert response.status_code == 202, response.text
    jobs.materialize_outbox(db)
    with db.control() as s:
        job = s.scalar(select(Job).where(Job.kind == "delete_account"))
        job_id = job.id
    assert execute(db, job_id, cfg) == "succeeded"
    with db.control() as s:
        assert (
            s.scalar(select(TradingDiscipline).where(TradingDiscipline.user_id == u["id"])) is None
        )
