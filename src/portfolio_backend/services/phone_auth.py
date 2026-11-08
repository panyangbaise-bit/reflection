"""Aliyun SMS and PNVS proofs; all identity decisions happen on the server."""

import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timezone
from threading import BoundedSemaphore
from uuid import uuid4

from alibabacloud_dypnsapi20170525.client import Client as PNVSClient
from alibabacloud_dypnsapi20170525.models import GetMobileRequest, VerifyMobileRequest
from alibabacloud_dysmsapi20170525.client import Client as SMSClient
from alibabacloud_dysmsapi20170525.models import SendSmsRequest
from alibabacloud_tea_openapi.models import Config
from alibabacloud_tea_util.models import RuntimeOptions
from redis.exceptions import RedisError
from sqlalchemy import select, text, update

from portfolio_backend.db import now
from portfolio_backend.errors import DomainError, require
from portfolio_backend.models import AuthSession, Portfolio, Preferences, User
from portfolio_backend.services.auth import digest, issue_tokens


class AliyunPhoneProvider:
    def __init__(self, config):
        self.config = config

    def client_config(self, endpoint):
        return Config(
            access_key_id=self.config.aliyun_access_key_id.get_secret_value(),
            access_key_secret=self.config.aliyun_access_key_secret.get_secret_value(),
            endpoint=endpoint,
            protocol="https",
            region_id="cn-hangzhou",
        )

    @staticmethod
    def runtime():
        # A timeout is ambiguous: never automatically repeat a billable SMS request.
        return RuntimeOptions(connect_timeout=3000, read_timeout=5000, autoretry=False)

    def send_code(self, phone, code):
        require(self.config.aliyun_sms_enabled, "sms_unavailable", "短信登录暂未开通", 503)
        try:
            response = SMSClient(self.client_config("dysmsapi.aliyuncs.com")).send_sms_with_options(
                SendSmsRequest(
                    phone_numbers=phone,
                    sign_name=self.config.aliyun_sms_sign_name,
                    template_code=self.config.aliyun_sms_template_code,
                    template_param=json.dumps({"code": code}),
                ),
                self.runtime(),
            )
            success = response.body is not None and response.body.code == "OK"
        except Exception:
            # Provider exceptions may contain credentials, phone numbers and request bodies.
            raise DomainError("sms_unavailable", "验证码发送失败，请稍后重试", 503) from None
        require(success, "sms_unavailable", "验证码发送失败，请稍后重试", 503)

    def mobile(self, token):
        require(
            self.config.aliyun_carrier_enabled,
            "carrier_unavailable",
            "请开启蜂窝网络重试，或使用密码登录",
            503,
        )
        try:
            response = PNVSClient(
                self.client_config("dypnsapi.aliyuncs.com")
            ).get_mobile_with_options(
                GetMobileRequest(access_token=token),
                self.runtime(),
            )
            body = response.body
            phone = (
                body.get_mobile_result_dto.mobile
                if body and body.code == "OK" and body.get_mobile_result_dto
                else ""
            )
        except Exception:
            raise DomainError(
                "carrier_unavailable", "本机号码验证失败，请开启蜂窝网络重试，或使用密码登录", 503
            ) from None
        require(
            bool(re.fullmatch(r"1[3-9][0-9]{9}", phone or "")),
            "invalid_carrier_token",
            "本机号码验证失败，请开启蜂窝网络重试，或使用密码登录",
            400,
        )
        return phone

    def verify_mobile(self, phone, token):
        require(
            self.config.aliyun_carrier_enabled, "carrier_unavailable", "本机号码认证暂不可用", 503
        )
        try:
            response = PNVSClient(
                self.client_config("dypnsapi.aliyuncs.com")
            ).verify_mobile_with_options(
                VerifyMobileRequest(access_code=token, phone_number=phone), self.runtime()
            )
            body = response.body
            passed = bool(
                body
                and body.code == "OK"
                and body.gate_verify_result_dto
                and body.gate_verify_result_dto.verify_result == "PASS"
            )
        except Exception:
            raise DomainError("carrier_unavailable", "本机号码校验失败，请稍后重试", 503) from None
        require(
            passed,
            "phone_mismatch",
            "本机号码与绑定号码不一致或无法认证，请切换为已绑定号码的 SIM 卡",
            403,
        )


# Keep memory-hard hashing within the small API container memory budget.
PASSWORD_SLOTS = BoundedSemaphore(2)


def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    require(PASSWORD_SLOTS.acquire(timeout=1), "auth_busy", "登录繁忙，请稍后重试", 503)
    try:
        value = hashlib.scrypt(password.encode(), salt=salt, n=32768, r=8, p=3, maxmem=67108864)
    finally:
        PASSWORD_SLOTS.release()
    return "scrypt-v1$" + salt.hex() + "$" + value.hex()


def verify_password(password, encoded):
    # Missing identities and unset passwords do the same expensive work.
    fallback = "scrypt-v1$" + "00" * 16 + "$" + "00" * 64
    try:
        version, salt, _ = (encoded or fallback).split("$")
        candidate = hash_password(password, bytes.fromhex(salt))
        return version == "scrypt-v1" and hmac.compare_digest(candidate, encoded or fallback)
    except (ValueError, TypeError):
        return False


RATE_SCRIPT = """
for i=1,#KEYS do
  local n=tonumber(redis.call('GET',KEYS[i]) or '0')
  if n>=tonumber(ARGV[(i-1)*2+1]) then return 0 end
end
for i=1,#KEYS do
  local n=redis.call('INCR',KEYS[i])
  if n==1 then redis.call('EXPIRE',KEYS[i],ARGV[(i-1)*2+2]) end
end
return 1
"""
VERIFY_SCRIPT = """
local raw=redis.call('GET',KEYS[1])
if not raw then return '' end
local item=cjson.decode(raw)
if item.purpose~=ARGV[1] or item.owner~=ARGV[2] then return '' end
item.attempts=item.attempts+1
if item.proof==ARGV[3] then redis.call('DEL',KEYS[1]); return item.phone end
if item.attempts>=5 then redis.call('DEL',KEYS[1])
else redis.call('SET',KEYS[1],cjson.encode(item),'KEEPTTL') end
return ''
"""


class PhoneAuthService:
    def __init__(self, db, config, cache, provider=None):
        self.db, self.config, self.cache = db, config, cache
        self.provider = provider or AliyunPhoneProvider(config)

    def mac(self, value):
        return hmac.new(
            self.config.phone_auth_pepper.get_secret_value().encode(),
            value.encode(),
            hashlib.sha256,
        ).hexdigest()

    def redis(self, method, *args, **kwargs):
        try:
            return getattr(self.cache, method)(*args, **kwargs)
        except RedisError:
            raise DomainError("auth_unavailable", "登录暂不可用，请稍后重试", 503) from None

    def rate(self, limits):
        keys = ["phone-auth:" + self.mac(key) for key, _, _ in limits]
        args = [value for _, limit, seconds in limits for value in (limit, seconds)]
        allowed = self.redis("eval", RATE_SCRIPT, len(keys), *keys, *args)
        require(allowed, "rate_limited", "操作过于频繁，请稍后重试", 429)

    def send_code(self, phone, ip, purpose="login", owner=""):
        require(self.config.aliyun_sms_enabled, "sms_unavailable", "短信登录暂未开通", 503)
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        self.rate(
            [
                (f"sms:minute:{phone}", 1, 60),
                (f"sms:day:{day}:{phone}", 10, 86400),
                (f"sms:ip:{ip}", 20, 3600),
                (f"sms:budget:{day}", self.config.sms_daily_limit, 86400),
            ]
        )
        challenge, code = str(uuid4()), f"{secrets.randbelow(1000000):06d}"
        key = "phone-code:" + challenge
        data = {
            "phone": phone,
            "purpose": purpose,
            "owner": str(owner),
            "attempts": 0,
            "proof": self.mac(challenge + ":" + code),
        }
        # Store before sending; remove on an ambiguous send failure and keep cooldown/budget.
        self.redis("set", key, json.dumps(data), ex=300)
        try:
            self.provider.send_code(phone, code)
        except Exception:
            self.redis("delete", key)
            raise
        return {"challenge_id": challenge, "expires_in": 300, "retry_after": 60}

    def verify_code(self, challenge, code, purpose="login", owner=""):
        require(
            self.config.aliyun_sms_enabled,
            "sms_unavailable",
            "短信认证已停用，请使用本机号码认证",
            503,
        )
        phone = self.redis(
            "eval",
            VERIFY_SCRIPT,
            1,
            "phone-code:" + str(challenge),
            purpose,
            str(owner),
            self.mac(str(challenge) + ":" + code),
        )
        require(phone, "invalid_code", "验证码错误或已过期，请重新获取", 400)
        return phone.decode() if isinstance(phone, bytes) else phone

    @staticmethod
    def lock_phone(session, phone):
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": "phone:" + phone},
        )

    def test_phone_login(self, phone):
        require(
            self.config.test_phone_login_enabled
            and self.config.store_environment == "Sandbox"
            and phone == "18000000000",
            "not_found",
            "无可用测试登录",
            404,
        )
        # Never claim a pre-existing real phone identity or attach to a caller's session.
        marker = "test-phone:" + phone
        with self.db.control() as s:
            self.lock_phone(s, phone)
            user = s.scalar(select(User).where(User.phone == phone).with_for_update())
            if user is None:
                user = User(phone=phone, apple_subject=marker, store_environment="Sandbox")
                s.add(user)
                s.flush()
                s.add_all([Portfolio(user_id=user.id), Preferences(user_id=user.id)])
            require(
                user.apple_subject == marker and user.store_environment == "Sandbox",
                "test_phone_conflict",
                "该号码已属于其他账户，不能作为免验证测试账户",
                409,
            )
            require(user.status == "active", "account_unavailable", "测试账户不可用", 403)
            return issue_tokens(s, user.id, self.config)

    def login_phone(self, phone):
        with self.db.control() as s:
            self.lock_phone(s, phone)
            user = s.scalar(select(User).where(User.phone == phone).with_for_update())
            if user is None:
                user = User(phone=phone, store_environment=self.config.store_environment)
                s.add(user)
                s.flush()
                s.add_all([Portfolio(user_id=user.id), Preferences(user_id=user.id)])
            require(user.status == "active", "account_unavailable", "账户不可用", 403)
            if not user.password_hash and user.apple_subject != "test-phone:" + phone:
                user.password_setup_required = True
            tokens = issue_tokens(s, user.id, self.config)
            if user.password_setup_required:
                session_id = s.scalar(
                    select(AuthSession.id).where(
                        AuthSession.access_hash == digest(tokens["access_token"])
                    )
                )
                self.redis("set", "password-setup:" + str(session_id), str(user.id), ex=600)
            return tokens

    def password_login(self, phone, password, ip):
        self.rate([(f"password:{phone}", 10, 900), (f"password-ip:{ip}", 30, 900)])
        with self.db.control() as s:
            user = s.scalar(select(User).where(User.phone == phone))
            encoded = user.password_hash if user else None
            user_id = user.id if user else None
        valid = verify_password(password, encoded)
        require(
            valid and user_id,
            "invalid_credentials",
            "手机号或密码不正确，可使用本机号码一键登录",
            401,
        )
        with self.db.control() as s:
            # Password reset/change may have happened while hashing outside the transaction.
            user = s.scalar(select(User).where(User.id == user_id).with_for_update())
            require(
                user
                and user.status == "active"
                and user.phone == phone
                and user.password_hash == encoded
                and not user.password_setup_required,
                "invalid_credentials",
                "手机号或密码不正确，可使用本机号码一键登录",
                401,
            )
            return issue_tokens(s, user.id, self.config)

    def setup_password(self, principal, password):
        proof = self.redis("get", "password-setup:" + str(principal.session_id))
        if isinstance(proof, bytes):
            proof = proof.decode()
        require(
            proof == str(principal.user_id),
            "phone_verification_required",
            "手机号验证已过期，请重新进行本机号码认证",
            403,
        )
        encoded = hash_password(password)
        with self.db.control() as s:
            user = s.scalar(select(User).where(User.id == principal.user_id).with_for_update())
            require(
                user
                and user.status == "active"
                and user.phone
                and user.password_setup_required
                and user.password_hash is None,
                "password_already_set",
                "请在设置中验证手机号后修改密码",
                409,
            )
            consumed = self.redis("getdel", "password-setup:" + str(principal.session_id))
            require(consumed, "phone_verification_required", "请重新进行本机号码认证", 403)
            user.password_hash = encoded
            user.password_setup_required = False
            s.execute(
                update(AuthSession)
                .where(AuthSession.user_id == user.id, AuthSession.id != principal.session_id)
                .values(revoked=True)
            )
        return {"updated": True}

    def sms_login(self, challenge, code):
        return self.login_phone(self.verify_code(challenge, code))

    def recovery_code(self, phone, password, new_phone, ip):
        # This recovery endpoint only authorizes SMS to the new number, never login itself.
        self.rate([(f"recovery:{phone}", 10, 900), (f"recovery-ip:{ip}", 30, 900)])
        with self.db.control() as s:
            user = s.scalar(select(User).where(User.phone == phone))
            encoded, user_id, status = (
                (user.password_hash, user.id, user.status) if user else (None, None, None)
            )
        valid = verify_password(password, encoded)
        require(
            valid and user_id and status == "active",
            "invalid_credentials",
            "原手机号或安全密码不正确，无法更换手机号",
            400,
        )
        return self.change_code(user_id, phone, encoded, new_phone, ip)

    def change_code(self, user_id, old_phone, encoded, new_phone, ip):
        require(old_phone != new_phone, "same_phone", "请输入新的手机号", 400)
        with self.db.control() as s:
            require(
                s.scalar(select(User.id).where(User.phone == new_phone)) is None,
                "phone_in_use",
                "新手机号已被其他账户使用",
                409,
            )
        result = self.send_code(new_phone, ip, "phone-change")
        metadata = {"user_id": str(user_id), "old_phone": old_phone, "password_hash": encoded}
        self.redis("set", "phone-change:" + result["challenge_id"], json.dumps(metadata), ex=300)
        return result

    def sms_change_code(self, principal, challenge, code, new_phone, ip):
        phone = self.verify_code(challenge, code, "phone-change-old", principal.user_id)
        with self.db.control() as s:
            user = s.get(User, principal.user_id)
            require(
                user and user.status == "active" and user.phone == phone,
                "invalid_code",
                "验证已失效，请重新验证原手机号",
                400,
            )
            encoded = user.password_hash
        return self.change_code(principal.user_id, phone, encoded, new_phone, ip)

    def change_phone(self, challenge, code):
        phone = self.verify_code(challenge, code, "phone-change")
        raw = self.redis("getdel", "phone-change:" + str(challenge))
        require(raw, "invalid_code", "换号请求已过期，请重新验证", 400)
        metadata = json.loads(raw)
        with self.db.control() as s:
            for number in sorted([metadata["old_phone"], phone]):
                self.lock_phone(s, number)
            user = s.scalar(select(User).where(User.id == metadata["user_id"]).with_for_update())
            require(
                user
                and user.status == "active"
                and user.phone == metadata["old_phone"]
                and user.password_hash == metadata["password_hash"],
                "recovery_changed",
                "账号信息已改变，请重新验证身份",
                409,
            )
            require(
                s.scalar(select(User.id).where(User.phone == phone)) is None,
                "phone_in_use",
                "新手机号已被其他账户使用",
                409,
            )
            user.phone = phone
            s.execute(
                update(AuthSession).where(AuthSession.user_id == user.id).values(revoked=True)
            )
            # A session is issued only after the NEW phone has passed SMS verification.
            return issue_tokens(s, user.id, self.config)

    def claim_carrier_token(self, token, ip):
        require(
            self.config.aliyun_carrier_enabled,
            "carrier_unavailable",
            "请开启蜂窝网络重试，或使用密码登录",
            503,
        )
        self.rate(
            [
                (f"carrier-ip:{ip}", 20, 600),
                ("carrier-budget", self.config.carrier_daily_limit, 86400),
            ]
        )
        fresh = self.redis("set", "carrier-token:" + self.mac(token), "1", ex=1800, nx=True)
        require(fresh, "invalid_carrier_token", "授权已使用，请重新获取本机号码授权", 400)

    def carrier_phone(self, token, ip):
        self.claim_carrier_token(token, ip)
        return self.provider.mobile(token)

    def carrier_login(self, token, ip):
        return self.login_phone(self.carrier_phone(token, ip))

    def carrier_bind(self, principal, token, ip):
        phone = self.carrier_phone(token, ip)
        with self.db.control() as s:
            self.lock_phone(s, phone)
            user = s.scalar(select(User).where(User.id == principal.user_id).with_for_update())
            require(user and user.status == "active", "account_unavailable", "账户不可用", 403)
            require(user.phone is None, "phone_already_bound", "已绑定手机号", 409)
            require(
                s.scalar(select(User.id).where(User.phone == phone)) is None,
                "phone_in_use",
                "该手机号已绑定其他账户",
                409,
            )
            user.phone = phone
            user.password_setup_required = user.password_hash is None
            if user.password_setup_required:
                self.redis(
                    "set", "password-setup:" + str(principal.session_id), str(user.id), ex=600
                )
        return {"updated": True}

    def carrier_verify_account(self, principal, token, ip, password=None):
        # Verify the server-owned number with VerifyMobile, not a client claim.
        phone = self.account_phone(principal)
        self.claim_carrier_token(token, ip)
        self.provider.verify_mobile(phone, token)
        encoded = hash_password(password) if password is not None else None
        with self.db.control() as s:
            user = s.scalar(select(User).where(User.id == principal.user_id).with_for_update())
            require(
                user and user.status == "active" and user.phone == phone,
                "phone_mismatch",
                "本机号码与绑定号码不一致，请切换为已绑定号码的 SIM 卡",
                403,
            )
            if encoded is not None:
                user.password_hash = encoded
                user.password_setup_required = False
                s.execute(
                    update(AuthSession)
                    .where(AuthSession.user_id == user.id, AuthSession.id != principal.session_id)
                    .values(revoked=True)
                )
            s.execute(
                update(AuthSession)
                .where(AuthSession.id == principal.session_id)
                .values(authenticated_at=now())
            )
        return {"updated": True}

    def carrier_change_phone(self, phone, password, token, ip):
        self.rate([(f"recovery:{phone}", 10, 900), (f"recovery-ip:{ip}", 30, 900)])
        with self.db.control() as s:
            user = s.scalar(select(User).where(User.phone == phone))
            encoded, user_id = (user.password_hash, user.id) if user else (None, None)
        require(
            verify_password(password, encoded) and user_id,
            "invalid_credentials",
            "原手机号或登录密码不正确",
            401,
        )
        new_phone = self.carrier_phone(token, ip)
        require(new_phone != phone, "same_phone", "请将蜂窝数据切换至新手机号的 SIM 卡", 400)
        with self.db.control() as s:
            for number in sorted([phone, new_phone]):
                self.lock_phone(s, number)
            user = s.scalar(select(User).where(User.id == user_id).with_for_update())
            require(
                user
                and user.status == "active"
                and user.phone == phone
                and user.password_hash == encoded
                and not user.password_setup_required,
                "recovery_changed",
                "账号信息已改变，请重新验证身份",
                409,
            )
            require(
                s.scalar(select(User.id).where(User.phone == new_phone)) is None,
                "phone_in_use",
                "新手机号已被其他账户使用",
                409,
            )
            user.phone = new_phone
            s.execute(
                update(AuthSession).where(AuthSession.user_id == user.id).values(revoked=True)
            )
            return issue_tokens(s, user.id, self.config)

    def account_phone(self, principal):
        with self.db.control() as s:
            user = s.get(User, principal.user_id)
            require(user and user.phone, "phone_required", "请先绑定手机号", 409)
            return user.phone

    def bind_phone(self, principal, challenge, code):
        phone = self.verify_code(challenge, code, "bind", principal.user_id)
        with self.db.control() as s:
            self.lock_phone(s, phone)
            user = s.scalar(select(User).where(User.id == principal.user_id).with_for_update())
            require(user and user.status == "active", "account_unavailable", "账户不可用", 403)
            require(user.phone is None, "phone_already_bound", "已绑定手机号", 409)
            existing = s.scalar(select(User.id).where(User.phone == phone))
            require(existing is None, "phone_in_use", "该手机号已绑定其他账户，请使用其他号码", 409)
            user.phone = phone
            user.password_setup_required = user.password_hash is None
            if user.password_setup_required:
                self.redis(
                    "set", "password-setup:" + str(principal.session_id), str(user.id), ex=600
                )
            return {"updated": True}

    def set_password(self, principal, challenge, code, password):
        phone = self.verify_code(challenge, code, "password", principal.user_id)
        encoded = hash_password(password)
        with self.db.control() as s:
            user = s.scalar(select(User).where(User.id == principal.user_id).with_for_update())
            require(
                user and user.status == "active" and user.phone == phone,
                "invalid_code",
                "验证失败，请重新获取验证码",
                400,
            )
            user.password_hash = encoded
            user.password_setup_required = False
            # Password reset invalidates all other devices, retaining the verified current session.
            s.execute(
                update(AuthSession)
                .where(AuthSession.user_id == user.id, AuthSession.id != principal.session_id)
                .values(revoked=True)
            )
            s.execute(
                update(AuthSession)
                .where(AuthSession.id == principal.session_id)
                .values(authenticated_at=now())
            )
        return {"updated": True}

    def reauthenticate(self, principal, challenge, code):
        phone = self.verify_code(challenge, code, "reauth", principal.user_id)
        with self.db.control() as s:
            user = s.scalar(select(User).where(User.id == principal.user_id).with_for_update())
            require(
                user and user.status == "active" and user.phone == phone,
                "invalid_code",
                "验证失败，请重新获取验证码",
                400,
            )
            s.execute(
                update(AuthSession)
                .where(AuthSession.id == principal.session_id)
                .values(authenticated_at=now())
            )
        return {"updated": True}
