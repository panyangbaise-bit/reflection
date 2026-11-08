from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from pydantic import ValidationError
from redis.exceptions import ConnectionError
from sqlalchemy import select

from portfolio_backend.config import Settings
from portfolio_backend.db import now
from portfolio_backend.errors import DomainError
from portfolio_backend.models import AuthSession, User
from portfolio_backend.schemas import PasswordSet
from portfolio_backend.services.phone_auth import (
    AliyunPhoneProvider,
    hash_password,
    verify_password,
)

M = "/api/mobile/v1"
PHONE = "13800138000"


class Provider:
    def __init__(self):
        self.sent = []
        self.tokens = []

    def send_code(self, phone, code):
        self.sent.append((phone, code))

    def mobile(self, token):
        self.tokens.append(token)
        return PHONE

    def verify_mobile(self, phone, token):
        if self.mobile(token) != phone:
            raise DomainError("phone_mismatch", "SIM does not match", 403)


@pytest.fixture
def phone_service(client):
    service = client.app.state.phone_auth
    service.config = service.config.model_copy(
        update={
            "aliyun_sms_enabled": True,
            "aliyun_carrier_enabled": True,
            "phone_auth_pepper": Settings(
                phone_auth_pepper="test-secret-not-for-production-0123456789"
            ).phone_auth_pepper,
        }
    )
    service.provider = Provider()
    return service


def send(client, service, phone=PHONE, path="/auth/sms/code", headers=None):
    response = client.post(
        M + path, headers=headers, json={"phone": phone, "agreements_accepted": True}
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert set(data) == {"challenge_id", "expires_in", "retry_after"}
    return {"challenge_id": data["challenge_id"], "code": service.provider.sent[-1][1]}


def test_sms_registration_same_identity_and_no_replay(client, phone_service):
    proof = send(client, phone_service)
    response = client.post(M + "/auth/sms/login", json=proof | {"agreements_accepted": True})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert (
        client.post(M + "/auth/sms/login", json=proof | {"agreements_accepted": True}).status_code
        == 400
    )
    carrier = client.post(
        M + "/auth/carrier/login",
        json={"access_token": "verified-token-123456789", "agreements_accepted": True},
    )
    assert carrier.json()["data"]["user_id"] == data["user_id"]
    me = client.get(M + "/me", headers={"Authorization": "Bearer " + data["access_token"]}).json()[
        "data"
    ]
    assert me["phone_masked"] == "138****8000" and me["has_password"] is False
    assert PHONE not in str(me)


def test_registration_requires_password_then_allows_password_or_sms_login(client, phone_service):
    proof = send(client, phone_service)
    login = client.post(M + "/auth/sms/login", json=proof | {"agreements_accepted": True}).json()[
        "data"
    ]
    headers = {"Authorization": "Bearer " + login["access_token"]}
    assert client.get(M + "/me", headers=headers).json()["data"]["password_setup_required"] is True
    assert client.get(M + "/home", headers=headers).status_code == 403
    assert (
        client.post(
            M + "/me/password/setup", headers=headers, json={"password": "short"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            M + "/me/password/setup", headers=headers, json={"password": "  long password  "}
        ).status_code
        == 200
    )
    assert len(phone_service.provider.sent) == 1  # Reuse the verified registration session.
    assert client.get(M + "/home", headers=headers).status_code == 200
    assert client.post(
        M + "/me/password/setup", headers=headers, json={"password": "another-password"}
    ).status_code in (403, 409)
    body = {"phone": PHONE, "password": "  long password  ", "agreements_accepted": True}
    response = client.post(M + "/auth/password/login", json=body)
    assert response.status_code == 200 and response.json()["data"]["user_id"] == login["user_id"]
    assert len(phone_service.provider.sent) == 1
    assert (
        client.post(
            M + "/auth/password/login", json=body | {"password": "long password"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            M + "/auth/password/login", json=body | {"agreements_accepted": False}
        ).status_code
        == 422
    )
    # Already registered users can still use SMS; they do not see setup again.
    phone_service.cache.flushdb()
    proof = send(client, phone_service)
    response = client.post(M + "/auth/sms/login", json=proof | {"agreements_accepted": True})
    headers = {"Authorization": "Bearer " + response.json()["data"]["access_token"]}
    assert client.get(M + "/me", headers=headers).json()["data"]["password_setup_required"] is False
    assert client.get(M + "/home", headers=headers).status_code == 200


def test_password_setup_requires_same_recent_phone_verified_session(client, phone_service, db):
    from portfolio_backend.services.auth import issue_tokens

    proof = send(client, phone_service)
    login = client.post(M + "/auth/sms/login", json=proof | {"agreements_accepted": True}).json()[
        "data"
    ]
    with db.control() as s:
        other = issue_tokens(s, login["user_id"], phone_service.config)
        sid = s.scalar(
            select(AuthSession.id).where(
                AuthSession.access_hash
                == __import__("hashlib").sha256(login["access_token"].encode()).hexdigest()
            )
        )
    body = {"password": "valid-new-password"}
    assert (
        client.post(
            M + "/me/password/setup",
            headers={"Authorization": "Bearer " + other["access_token"]},
            json=body,
        ).status_code
        == 403
    )
    phone_service.cache.delete("password-setup:" + str(sid))
    assert (
        client.post(
            M + "/me/password/setup",
            headers={"Authorization": "Bearer " + login["access_token"]},
            json=body,
        ).status_code
        == 403
    )


def test_password_login_has_uniform_errors_rate_limit_and_reset_race_check(
    client, phone_service, user, db, monkeypatch
):
    identity = user()
    with db.control() as s:
        u = s.get(User, identity["id"])
        u.phone, u.password_hash = PHONE, hash_password("correct-password")
    body = {"phone": PHONE, "password": "incorrect-password", "agreements_accepted": True}
    wrong = client.post(M + "/auth/password/login", json=body)
    missing = client.post(M + "/auth/password/login", json=body | {"phone": "13700137000"})
    assert wrong.status_code == missing.status_code == 401
    assert wrong.json()["error"]["message"] == missing.json()["error"]["message"]

    def reset_during_hash(password, encoded):
        with db.control() as s:
            s.get(User, identity["id"]).password_hash = hash_password("changed-password")
        return True

    monkeypatch.setattr("portfolio_backend.services.phone_auth.verify_password", reset_during_hash)
    assert (
        client.post(
            M + "/auth/password/login", json=body | {"password": "correct-password"}
        ).status_code
        == 401
    )
    monkeypatch.setattr(
        "portfolio_backend.services.phone_auth.verify_password", lambda *args: False
    )
    for _ in range(8):
        assert client.post(M + "/auth/password/login", json=body).status_code == 401
    assert client.post(M + "/auth/password/login", json=body).status_code == 429


def test_concurrent_proof_consumption_only_succeeds_once(client, phone_service):
    proof = send(client, phone_service)

    def consume(_):
        try:
            return phone_service.verify_code(proof["challenge_id"], proof["code"])
        except DomainError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(consume, range(2)))
    assert results.count(PHONE) == 1


def test_wrong_code_limit_expiry_and_cooldown(client, phone_service):
    proof = send(client, phone_service)
    assert (
        client.post(
            M + "/auth/sms/code", json={"phone": PHONE, "agreements_accepted": True}
        ).status_code
        == 429
    )
    wrong = "000000" if proof["code"] != "000000" else "000001"
    for _ in range(5):
        assert (
            client.post(
                M + "/auth/sms/login", json=proof | {"code": wrong, "agreements_accepted": True}
            ).status_code
            == 400
        )
    assert (
        client.post(M + "/auth/sms/login", json=proof | {"agreements_accepted": True}).status_code
        == 400
    )
    second = send(client, phone_service, "13900139000")
    phone_service.cache.delete("phone-code:" + second["challenge_id"])
    assert (
        client.post(M + "/auth/sms/login", json=second | {"agreements_accepted": True}).status_code
        == 400
    )


def test_consent_validation_and_apple_routes_removed(client, phone_service):
    for phone in ["+8613800138000", "1380013800", "abc", "１３８００１３８０００"]:
        assert (
            client.post(
                M + "/auth/sms/code", json={"phone": phone, "agreements_accepted": True}
            ).status_code
            == 422
        )
    assert (
        client.post(
            M + "/auth/sms/code", json={"phone": PHONE, "agreements_accepted": False}
        ).status_code
        == 422
    )
    assert not phone_service.provider.sent
    assert client.post(M + "/auth/apple", json={}).status_code == 404
    assert client.post(M + "/auth/challenges").status_code == 404


def test_bind_preserves_existing_user_and_proof_is_scoped(client, phone_service, user, db):
    old, other = user(), user()
    with db.control() as s:
        s.get(User, old["id"]).phone = None
    proof = send(client, phone_service, path="/me/phone/code", headers=old["headers"])
    assert (
        client.post(M + "/auth/sms/login", json=proof | {"agreements_accepted": True}).status_code
        == 400
    )
    assert client.post(M + "/me/phone", headers=other["headers"], json=proof).status_code == 400
    assert client.post(M + "/me/phone", headers=old["headers"], json=proof).status_code == 200
    assert client.get(M + "/me", headers=old["headers"]).json()["data"]["id"] == str(old["id"])
    assert phone_service.login_phone(PHONE)["user_id"] == str(old["id"])


def test_binding_cannot_merge_or_replace_account(client, phone_service, user, db):
    old, other = user(), user()
    with db.control() as s:
        s.get(User, old["id"]).phone = None
        s.get(User, other["id"]).phone = PHONE
    proof = send(client, phone_service, path="/me/phone/code", headers=old["headers"])
    assert client.post(M + "/me/phone", headers=old["headers"], json=proof).status_code == 409
    assert (
        client.post(
            M + "/me/phone/code",
            headers=other["headers"],
            json={"phone": "13900139000", "agreements_accepted": True},
        ).status_code
        == 409
    )


def test_password_reset_requires_otp_and_revokes_other_sessions(client, phone_service, user, db):
    old = user()
    with db.control() as s:
        s.get(User, old["id"]).phone = PHONE
    other = phone_service.login_phone(PHONE)
    sent = client.post(M + "/me/password/code", headers=old["headers"])
    assert sent.status_code == 201, sent.text
    proof = {
        "challenge_id": sent.json()["data"]["challenge_id"],
        "code": phone_service.provider.sent[-1][1],
    }
    password = "  correct password  "
    response = client.post(
        M + "/me/password", headers=old["headers"], json=proof | {"password": password}
    )
    assert response.status_code == 200, response.text
    assert (
        client.get(
            M + "/me", headers={"Authorization": "Bearer " + other["access_token"]}
        ).status_code
        == 401
    )
    assert client.get(M + "/me", headers=old["headers"]).json()["data"]["has_password"] is True
    assert (
        client.post(
            M + "/auth/password/login",
            json={"phone": PHONE, "password": password, "agreements_accepted": True},
        ).status_code
        == 200
    )
    assert (
        client.post(
            M + "/auth/phone/recovery-code",
            json={
                "phone": PHONE,
                "password": password.strip(),
                "new_phone": "13700137000",
                "agreements_accepted": True,
            },
        ).status_code
        == 400
    )
    assert (
        client.post(
            M + "/me/password", headers=old["headers"], json=proof | {"password": password}
        ).status_code
        == 400
    )
    with db.control() as s:
        assert password not in s.get(User, old["id"]).password_hash


def test_reauth_is_same_account_and_allows_deletion(client, phone_service, user, db):
    old = user()
    with db.control() as s:
        s.get(User, old["id"]).phone = PHONE
        s.scalar(select(AuthSession).where(AuthSession.user_id == old["id"])).authenticated_at = (
            now() - timedelta(hours=1)
        )
    assert (
        client.post(
            M + "/me/deletion-requests", headers=old["headers"], json={"confirmation": "DELETE"}
        ).status_code
        == 403
    )
    sent = client.post(M + "/me/reauth/code", headers=old["headers"])
    proof = {
        "challenge_id": sent.json()["data"]["challenge_id"],
        "code": phone_service.provider.sent[-1][1],
    }
    assert client.post(M + "/me/reauth", headers=old["headers"], json=proof).status_code == 200
    assert (
        client.post(
            M + "/me/deletion-requests", headers=old["headers"], json={"confirmation": "DELETE"}
        ).status_code
        == 202
    )


def test_carrier_replay_and_no_client_phone_override(client, phone_service):
    body = {"access_token": "single-use-carrier-token-123", "agreements_accepted": True}
    assert client.post(M + "/auth/carrier/login", json=body | {"phone": PHONE}).status_code == 422
    assert client.post(M + "/auth/carrier/login", json=body).status_code == 200
    assert client.post(M + "/auth/carrier/login", json=body).status_code == 400
    assert len(phone_service.provider.tokens) == 1


def test_services_disabled_and_redis_failure_fail_closed(client, phone_service, monkeypatch):
    phone_service.config = phone_service.config.model_copy(
        update={"aliyun_sms_enabled": False, "aliyun_carrier_enabled": False}
    )
    assert (
        client.post(
            M + "/auth/sms/code", json={"phone": PHONE, "agreements_accepted": True}
        ).status_code
        == 503
    )
    assert (
        client.post(
            M + "/auth/carrier/login",
            json={"access_token": "token-12345678901234567890", "agreements_accepted": True},
        ).status_code
        == 503
    )
    assert not phone_service.provider.sent and not phone_service.provider.tokens
    phone_service.config = phone_service.config.model_copy(update={"aliyun_sms_enabled": True})

    def unavailable(*args, **kwargs):
        raise ConnectionError("unavailable")

    monkeypatch.setattr(phone_service.cache, "eval", unavailable)
    with pytest.raises(DomainError) as exc:
        phone_service.send_code(PHONE, "local")
    assert exc.value.status == 503
    assert not phone_service.provider.sent


def test_global_sms_budget_before_billable_request(client, phone_service):
    phone_service.config = phone_service.config.model_copy(update={"sms_daily_limit": 1})
    send(client, phone_service)
    response = client.post(
        M + "/auth/sms/code", json={"phone": "13900139000", "agreements_accepted": True}
    )
    assert response.status_code == 429
    assert len(phone_service.provider.sent) == 1


def test_hashes_salted_and_password_whitespace_preserved():
    password = "  password  "
    a, b = hash_password(password), hash_password(password)
    assert a != b and verify_password(password, a)
    assert not verify_password(password.strip(), a)
    assert not verify_password(password, None)
    parsed = PasswordSet(
        challenge_id="00000000-0000-0000-0000-000000000001", code="123456", password=password
    )
    assert parsed.password == password
    with pytest.raises(ValidationError):
        Settings(aliyun_sms_enabled=True)


def test_aliyun_sdk_adapter_uses_https_and_never_exposes_provider_errors(monkeypatch):
    from alibabacloud_tea_openapi.client import Client

    config = Settings(
        aliyun_sms_enabled=True,
        aliyun_carrier_enabled=True,
        aliyun_access_key_id="test-key",
        aliyun_access_key_secret="test-secret",
        aliyun_sms_sign_name="测试",
        aliyun_sms_template_code="SMS_TEST",
        phone_auth_pepper="test-pepper-at-least-32-characters-long",
    )
    provider = AliyunPhoneProvider(config)
    calls = []

    def call_api(self, params, request, runtime):
        calls.append(params.action)
        assert params.protocol == "HTTPS" and params.version == "2017-05-25"
        assert runtime.autoretry is False and runtime.read_timeout == 5000
        if params.action == "SendSms":
            assert request.query["PhoneNumbers"] == PHONE
            assert request.query["TemplateParam"] == '{"code": "123456"}'
            return {"body": {"Code": "OK"}, "statusCode": 200}
        assert request.query["AccessToken"] == "sdk-consent-token"
        return {"body": {"Code": "OK", "GetMobileResultDTO": {"Mobile": PHONE}}, "statusCode": 200}

    monkeypatch.setattr(Client, "call_api", call_api)
    provider.send_code(PHONE, "123456")
    assert provider.mobile("sdk-consent-token") == PHONE
    assert calls == ["SendSms", "GetMobile"]

    def failure(*args):
        raise RuntimeError("test-secret " + PHONE)

    monkeypatch.setattr(Client, "call_api", failure)
    with pytest.raises(DomainError) as exc:
        provider.send_code(PHONE, "123456")
    assert "test-secret" not in exc.value.message and PHONE not in exc.value.message


def test_unbound_session_cannot_read_home_until_phone_verified(client, phone_service, user, db):
    old = user()
    with db.control() as s:
        s.get(User, old["id"]).phone = None
    assert client.get(M + "/me", headers=old["headers"]).status_code == 200
    for path in ["/home", "/accounts", "/instruments", "/feed"]:
        response = client.get(M + path, headers=old["headers"])
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "phone_required"
    proof = send(client, phone_service, path="/me/phone/code", headers=old["headers"])
    assert client.post(M + "/me/phone", headers=old["headers"], json=proof).status_code == 200
    assert client.get(M + "/home", headers=old["headers"]).status_code == 403
    setup = client.post(
        M + "/me/password/setup", headers=old["headers"], json={"password": "initial-password"}
    )
    assert setup.status_code == 200
    assert client.get(M + "/home", headers=old["headers"]).status_code == 200


def test_recovery_password_only_authorizes_new_phone_sms(client, phone_service, user, db):
    old = user()
    password = "  recovery password  "
    with db.control() as s:
        u = s.get(User, old["id"])
        u.phone, u.password_hash = PHONE, hash_password(password)
    body = {
        "phone": PHONE,
        "password": password,
        "new_phone": "13700137000",
        "agreements_accepted": True,
    }
    response = client.post(M + "/auth/phone/recovery-code", json=body)
    assert response.status_code == 201, response.text
    assert set(response.json()["data"]) == {"challenge_id", "expires_in", "retry_after"}
    assert phone_service.provider.sent[-1][0] == "13700137000"
    proof = {
        "challenge_id": response.json()["data"]["challenge_id"],
        "code": phone_service.provider.sent[-1][1],
        "agreements_accepted": True,
    }
    assert client.post(M + "/auth/sms/login", json=proof).status_code == 400
    wrong = "000000" if proof["code"] != "000000" else "000001"
    assert client.post(M + "/auth/phone/change", json=proof | {"code": wrong}).status_code == 400
    assert (
        client.get(M + "/me", headers=old["headers"]).json()["data"]["phone_masked"]
        == "138****8000"
    )
    changed = client.post(M + "/auth/phone/change", json=proof)
    assert changed.status_code == 200, changed.text
    tokens = changed.json()["data"]
    assert tokens["user_id"] == str(old["id"])
    assert client.get(M + "/me", headers=old["headers"]).status_code == 401
    headers = {"Authorization": "Bearer " + tokens["access_token"]}
    assert client.get(M + "/me", headers=headers).json()["data"]["phone_masked"] == "137****7000"
    accounts = client.get(M + "/accounts", headers=headers).json()["data"]
    assert accounts[0]["id"] == str(old["account_id"])
    assert client.post(M + "/auth/phone/change", json=proof).status_code == 400
    assert (
        client.post(
            M + "/auth/phone/recovery-code", json=body | {"new_phone": "13600136000"}
        ).status_code
        == 400
    )


def test_recovery_requires_password_and_rejects_changed_account_or_claimed_phone(
    client, phone_service, user, db
):
    old, other = user(), user()
    body = {
        "phone": PHONE,
        "password": "correct-password",
        "new_phone": "13700137000",
        "agreements_accepted": True,
    }
    with db.control() as s:
        s.get(User, old["id"]).phone = PHONE
    assert client.post(M + "/auth/phone/recovery-code", json=body).status_code == 400
    with db.control() as s:
        s.get(User, old["id"]).password_hash = hash_password(body["password"])
        s.get(User, other["id"]).phone = body["new_phone"]
    assert client.post(M + "/auth/phone/recovery-code", json=body).status_code == 409
    body["new_phone"] = "13600136000"
    response = client.post(M + "/auth/phone/recovery-code", json=body)
    proof = {
        "challenge_id": response.json()["data"]["challenge_id"],
        "code": phone_service.provider.sent[-1][1],
        "agreements_accepted": True,
    }
    with db.control() as s:
        s.get(User, old["id"]).password_hash = hash_password("a-new-password")
    assert client.post(M + "/auth/phone/change", json=proof).status_code == 409
    assert (
        client.get(M + "/me", headers=old["headers"]).json()["data"]["phone_masked"]
        == "138****8000"
    )


def test_change_phone_with_old_and_new_sms_does_not_require_password(
    client, phone_service, user, db
):
    old = user()
    with db.control() as s:
        s.get(User, old["id"]).phone = PHONE
    first = client.post(M + "/me/phone/old-code", headers=old["headers"])
    proof = {
        "challenge_id": first.json()["data"]["challenge_id"],
        "code": phone_service.provider.sent[-1][1],
        "new_phone": "13700137000",
    }
    second = client.post(M + "/me/phone/change-code", headers=old["headers"], json=proof)
    assert second.status_code == 201, second.text
    final = {
        "challenge_id": second.json()["data"]["challenge_id"],
        "code": phone_service.provider.sent[-1][1],
        "agreements_accepted": True,
    }
    response = client.post(M + "/auth/phone/change", json=final)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["user_id"] == str(old["id"])


def test_test_phone_login_requires_explicit_sandbox_opt_in_and_consent(client, phone_service):
    body = {"phone": "18000000000", "agreements_accepted": True}
    assert client.post(M + "/auth/test-phone/login", json=body).status_code == 404
    phone_service.config = phone_service.config.model_copy(
        update={"test_phone_login_enabled": True}
    )
    assert (
        client.post(
            M + "/auth/test-phone/login", json=body | {"agreements_accepted": False}
        ).status_code
        == 422
    )
    assert (
        client.post(M + "/auth/test-phone/login", json=body | {"phone": "18000000006"}).status_code
        == 404
    )
    first = client.post(M + "/auth/test-phone/login", json=body)
    second = client.post(M + "/auth/test-phone/login", json=body)
    assert first.status_code == second.status_code == 200
    assert first.json()["data"]["user_id"] == second.json()["data"]["user_id"]
    headers = {"Authorization": "Bearer " + first.json()["data"]["access_token"]}
    assert client.get(M + "/me", headers=headers).json()["data"]["phone_masked"] == "180****0000"
    assert client.get(M + "/home", headers=headers).status_code == 200
    assert not phone_service.provider.sent and not phone_service.provider.tokens
    phone_service.config = phone_service.config.model_copy(
        update={"store_environment": "Production"}
    )
    assert client.post(M + "/auth/test-phone/login", json=body).status_code == 404
    with pytest.raises(ValidationError):
        Settings(test_phone_login_enabled=True, store_environment="Production")


def test_test_phone_cannot_claim_existing_account(client, phone_service, user, db):
    owner = user()
    with db.control() as s:
        s.get(User, owner["id"]).phone = "18000000000"
    phone_service.config = phone_service.config.model_copy(
        update={"test_phone_login_enabled": True}
    )
    assert (
        client.post(
            M + "/auth/test-phone/login", json={"phone": "18000000000", "agreements_accepted": True}
        ).status_code
        == 409
    )
    assert client.get(M + "/me", headers=owner["headers"]).status_code == 200


def carrier_body(token="carrier-test-authorized-token-001"):
    return {"access_token": token, "agreements_accepted": True}


def carrier_session(client, service):
    service.config = service.config.model_copy(update={"aliyun_sms_enabled": False})
    response = client.post(M + "/auth/carrier/login", json=carrier_body())
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    headers = {"Authorization": "Bearer " + data["access_token"]}
    setup = client.post(
        M + "/me/password/setup", headers=headers, json={"password": "initial-carrier-password"}
    )
    assert setup.status_code == 200, setup.text
    return data, headers


def test_carrier_only_registration_password_reset_and_reauth(client, phone_service):
    data, headers = carrier_session(client, phone_service)
    assert not phone_service.provider.sent
    other = client.post(
        M + "/auth/password/login",
        json={"phone": PHONE, "password": "initial-carrier-password", "agreements_accepted": True},
    )
    assert other.status_code == 200, other.text
    other_headers = {"Authorization": "Bearer " + other.json()["data"]["access_token"]}
    reset = client.post(
        M + "/me/password/carrier",
        headers=headers,
        json=carrier_body("carrier-reset-token-0012345") | {"password": "updated-carrier-password"},
    )
    assert reset.status_code == 200, reset.text
    assert client.get(M + "/me", headers=other_headers).status_code == 401
    assert (
        client.post(
            M + "/me/reauth/carrier",
            headers=headers,
            json=carrier_body("carrier-reauth-token-0012345"),
        ).status_code
        == 200
    )
    # The same proof cannot be reused across login/reset/reauth routes.
    assert (
        client.post(
            M + "/auth/carrier/login", json=carrier_body("carrier-reset-token-0012345")
        ).status_code
        == 400
    )
    assert (
        client.post(
            M + "/auth/password/login",
            json={
                "phone": PHONE,
                "password": "updated-carrier-password",
                "agreements_accepted": True,
            },
        ).status_code
        == 200
    )
    assert (
        client.post(
            M + "/auth/sms/login",
            json={
                "challenge_id": "00000000-0000-0000-0000-000000000001",
                "code": "123456",
                "agreements_accepted": True,
            },
        ).status_code
        == 503
    )


def test_carrier_reset_rejects_wrong_sim_and_missing_consent(client, phone_service):
    _, headers = carrier_session(client, phone_service)
    phone_service.provider.mobile = lambda token: "13900139000"
    reset = carrier_body("wrong-sim-token-123456789") | {"password": "updated-carrier-password"}
    assert client.post(M + "/me/password/carrier", headers=headers, json=reset).status_code == 403
    assert (
        client.post(
            M + "/me/reauth/carrier",
            headers=headers,
            json=carrier_body("wrong-sim-reauth-123456789"),
        ).status_code
        == 403
    )
    assert (
        client.post(
            M + "/me/password/carrier", headers=headers, json=reset | {"agreements_accepted": False}
        ).status_code
        == 422
    )
    assert (
        client.post(
            M + "/auth/password/login",
            json={
                "phone": PHONE,
                "password": "initial-carrier-password",
                "agreements_accepted": True,
            },
        ).status_code
        == 200
    )


def test_carrier_change_preserves_identity_revokes_sessions_and_rejects_takeover(
    client, phone_service, user, db
):
    data, headers = carrier_session(client, phone_service)
    other = user()
    with db.control() as s:
        occupied = s.get(User, other["id"]).phone
    phone_service.provider.mobile = lambda token: occupied
    body = {"phone": PHONE, "password": "initial-carrier-password"} | carrier_body(
        "occupied-carrier-12345678"
    )
    assert client.post(M + "/auth/carrier/change", json=body).status_code == 409
    phone_service.provider.mobile = lambda token: "13900139000"
    assert (
        client.post(
            M + "/auth/carrier/change", json=body | {"password": "incorrect-password"}
        ).status_code
        == 401
    )
    changed = client.post(
        M + "/auth/carrier/change", json=body | carrier_body("new-sim-carrier-123456789")
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["data"]["user_id"] == data["user_id"]
    assert client.get(M + "/me", headers=headers).status_code == 401
    assert (
        client.post(
            M + "/auth/password/login",
            json={
                "phone": "13900139000",
                "password": "initial-carrier-password",
                "agreements_accepted": True,
            },
        ).status_code
        == 200
    )
    assert not phone_service.provider.sent


def test_carrier_legacy_binding_requires_setup_and_does_not_claim_existing_phone(
    client, phone_service, user, db
):
    from sqlalchemy import update

    u = user()
    with db.control() as s:
        s.execute(update(User).where(User.id == u["id"]).values(phone=None))
    phone_service.config = phone_service.config.model_copy(update={"aliyun_sms_enabled": False})
    binding = client.post(M + "/me/phone/carrier", headers=u["headers"], json=carrier_body())
    assert binding.status_code == 200, binding.text
    assert client.get(M + "/me", headers=u["headers"]).json()["data"]["password_setup_required"]
    assert client.get(M + "/home", headers=u["headers"]).status_code == 403
    assert (
        client.post(
            M + "/me/password/setup",
            headers=u["headers"],
            json={"password": "legacy-initial-password"},
        ).status_code
        == 200
    )
    assert client.get(M + "/home", headers=u["headers"]).status_code == 200
    other = user()
    with db.control() as s:
        s.execute(update(User).where(User.id == other["id"]).values(phone=None))
    assert (
        client.post(
            M + "/me/phone/carrier",
            headers=other["headers"],
            json=carrier_body("claimed-carrier-token-123456"),
        ).status_code
        == 409
    )


def test_carrier_change_checks_password_again_after_provider_call(client, phone_service, db):
    data, _ = carrier_session(client, phone_service)
    from uuid import UUID

    def racing_provider(token):
        with db.control() as s:
            s.get(User, UUID(data["user_id"])).password_hash = hash_password(
                "changed-during-provider"
            )
        return "13900139000"

    phone_service.provider.mobile = racing_provider
    changed = client.post(
        M + "/auth/carrier/change",
        json=carrier_body("racing-carrier-token-123456")
        | {"phone": PHONE, "password": "initial-carrier-password"},
    )
    assert changed.status_code == 409


@pytest.mark.parametrize("verdict", ["PASS", "REJECT", "UNKNOWN"])
def test_aliyun_verify_mobile_requires_pass(monkeypatch, verdict):
    from alibabacloud_tea_openapi.client import Client

    provider = AliyunPhoneProvider(
        Settings(
            aliyun_carrier_enabled=True,
            aliyun_access_key_id="test-key",
            aliyun_access_key_secret="test-secret",
            phone_auth_pepper="test-pepper-at-least-32-characters-long",
        )
    )

    def call_api(self, params, request, runtime):
        assert params.action == "VerifyMobile" and params.protocol == "HTTPS"
        assert request.query["PhoneNumber"] == PHONE
        assert request.query["AccessCode"] == "verification-sdk-token"
        assert runtime.autoretry is False
        return {
            "body": {"Code": "OK", "GateVerifyResultDTO": {"VerifyResult": verdict}},
            "statusCode": 200,
        }

    monkeypatch.setattr(Client, "call_api", call_api)
    if verdict == "PASS":
        provider.verify_mobile(PHONE, "verification-sdk-token")
    else:
        with pytest.raises(DomainError) as exc:
            provider.verify_mobile(PHONE, "verification-sdk-token")
        assert exc.value.code == "phone_mismatch"
