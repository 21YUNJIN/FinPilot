"""身份、会话与强验证因子（V5 6.2、SYS-02、AUTH-05）。

- 身份只来自服务端会话；请求体与模型输出不能覆盖 user_id。
- 密码使用 scrypt 加盐哈希；TOTP 秘钥加密保存，验证码不进入日志、审计或模型上下文。
- TOTP 实现 RFC 6238（HMAC-SHA1、30 秒步长、默认 ±1 窗口），已消费的计数器不可复用。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
from dataclasses import dataclass
from datetime import datetime, timedelta

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..errors import FinPilotError
from ..ids import new_token
from ..models import SessionToken, User
from ..timeutil import utcnow

_TOTP_STEP_SECONDS = 30
_TOTP_DIGITS = 6
_TOTP_WINDOW = 1


@dataclass(frozen=True)
class AuthContext:
    """可信身份上下文。只在服务端传递，不由客户端声明。"""

    user_id: str
    roles: tuple[str, ...]
    channel: str
    session_id: str

    @property
    def is_operator(self) -> bool:
        return "operator" in self.roles or "risk_officer" in self.roles

    @property
    def is_customer(self) -> bool:
        return "customer" in self.roles

    def require_role(self, role: str) -> None:
        if role not in self.roles:
            raise FinPilotError("FORBIDDEN", "当前身份没有执行该操作的权限。")

    def require_customer(self) -> None:
        if not self.is_customer:
            raise FinPilotError("FORBIDDEN", "该操作仅限客户本人执行。")


# --- 密码 ---------------------------------------------------------------

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_LEN = 32


def hash_password(password: str) -> str:
    if not password or len(password) < 8:
        raise FinPilotError("PARAM_INVALID", "密码至少 8 位。")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_LEN)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    if not encoded or not encoded.startswith("scrypt$"):
        return False
    try:
        _, n, r, p, salt_hex, digest_hex = encoded.split("$")
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(digest_hex)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def set_password(session: Session, user: User, password: str) -> None:
    user.password_hash = hash_password(password)
    session.flush()


def register_user(
    session: Session,
    user_id: str,
    display_name: str,
    password: str,
    *,
    phone_ref: str = "",
    roles: list[str] | None = None,
) -> User:
    existing = session.get(User, user_id)
    if existing is not None:
        raise FinPilotError("PARAM_INVALID", "该用户已存在。")
    user = User(
        user_id=user_id,
        status="active",
        display_name=display_name,
        phone_ref=phone_ref,
        password_hash=hash_password(password),
        roles=roles or ["customer"],
        created_at=utcnow(),
    )
    session.add(user)
    session.flush()
    return user


# --- 会话 ---------------------------------------------------------------


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(session: Session, user_id: str, password: str, channel: str) -> tuple[str, AuthContext]:
    user = session.get(User, user_id)
    if user is None or user.status != "active":
        # 不泄露用户是否存在
        raise FinPilotError("UNAUTHENTICATED", "账号或密码不正确。")
    if not verify_password(password, user.password_hash):
        raise FinPilotError("UNAUTHENTICATED", "账号或密码不正确。")
    settings = get_settings()
    token = new_token()
    now = utcnow()
    record = SessionToken(
        user_id=user_id,
        channel=channel,
        token_hash=_token_hash(token),
        created_at=now,
        expires_at=now + timedelta(minutes=settings.session_ttl_minutes),
    )
    session.add(record)
    session.flush()
    ctx = AuthContext(
        user_id=user_id,
        roles=tuple(user.roles or ["customer"]),
        channel=channel,
        session_id=record.session_id,
    )
    return token, ctx


def authenticate(session: Session, token: str) -> AuthContext:
    if not token:
        raise FinPilotError("UNAUTHENTICATED", "缺少登录凭证。")
    record = session.execute(
        select(SessionToken).where(SessionToken.token_hash == _token_hash(token))
    ).scalar_one_or_none()
    if record is None or record.revoked_at is not None:
        raise FinPilotError("UNAUTHENTICATED", "登录状态无效，请重新登录。")
    now = utcnow()
    if record.expires_at <= now:
        raise FinPilotError("UNAUTHENTICATED", "登录已过期，请重新登录。")
    user = session.get(User, record.user_id)
    if user is None or user.status != "active":
        raise FinPilotError("UNAUTHENTICATED", "账号状态异常。")
    record.last_seen_at = now
    session.flush()
    return AuthContext(
        user_id=record.user_id,
        roles=tuple(user.roles or ["customer"]),
        channel=record.channel,
        session_id=record.session_id,
    )


def revoke_session(session: Session, session_id: str) -> None:
    record = session.get(SessionToken, session_id)
    if record is not None and record.revoked_at is None:
        record.revoked_at = utcnow()
        session.flush()


# --- TOTP 强验证因子 -----------------------------------------------------


def _fernet() -> Fernet:
    key = hashlib.sha256(get_settings().totp_encryption_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_secret(secret: str) -> str:
    return _fernet().encrypt(secret.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise FinPilotError("SYSTEM_ERROR", "验证因子材料无法解密。") from exc


def generate_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def totp_at(secret: str, moment: datetime, *, step: int = _TOTP_STEP_SECONDS, digits: int = _TOTP_DIGITS) -> tuple[str, int]:
    """返回 (验证码, 时间计数器)。"""
    padding = "=" * (-len(secret) % 8)
    key = base64.b32decode(secret + padding, casefold=True)
    counter = int(moment.timestamp()) // step
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    code = str(binary % (10**digits)).zfill(digits)
    return code, counter


def verify_totp(
    secret: str,
    code: str,
    moment: datetime,
    *,
    window: int = _TOTP_WINDOW,
    used_counters: set[int] | None = None,
) -> tuple[bool, int | None]:
    """校验 TOTP，并返回命中的时间计数器；已消费的计数器视为重放。"""
    if not code or not code.isdigit() or len(code) != _TOTP_DIGITS:
        return False, None
    used = used_counters or set()
    for offset in range(-window, window + 1):
        candidate_code, counter = totp_at(secret, moment + timedelta(seconds=offset * _TOTP_STEP_SECONDS))
        if counter in used:
            continue
        if hmac.compare_digest(candidate_code, code):
            return True, counter
    return False, None


def otpauth_uri(user_id: str, secret: str, issuer: str = "FinPilot") -> str:
    return f"otpauth://totp/{issuer}:{user_id}?secret={secret}&issuer={issuer}&period={_TOTP_STEP_SECONDS}&digits={_TOTP_DIGITS}"


def enroll_totp(session: Session, ctx: AuthContext) -> dict:
    ctx.require_customer()
    user = session.get(User, ctx.user_id)
    if user is None:
        raise FinPilotError("UNAUTHENTICATED", "用户不存在。")
    secret = generate_totp_secret()
    user.totp_secret_enc = encrypt_secret(secret)
    user.totp_confirmed_at = None
    session.flush()
    # 秘钥只在注册流程中返回一次，用于写入独立验证器；不进入对话与日志
    return {
        "user_id": user.user_id,
        "secret": secret,
        "otpauth_uri": otpauth_uri(user.user_id, secret),
        "confirmed": False,
    }


def confirm_totp(session: Session, ctx: AuthContext, code: str) -> bool:
    ctx.require_customer()
    user = session.get(User, ctx.user_id)
    if user is None or not user.totp_secret_enc:
        raise FinPilotError("PARAM_INVALID", "尚未发起验证因子注册。")
    secret = decrypt_secret(user.totp_secret_enc)
    ok, _counter = verify_totp(secret, code, utcnow())
    if not ok:
        return False
    user.totp_confirmed_at = utcnow()
    session.flush()
    return True


def user_has_strong_factor(session: Session, user_id: str) -> bool:
    user = session.get(User, user_id)
    return bool(user and user.totp_secret_enc and user.totp_confirmed_at)


def load_totp_secret(session: Session, user_id: str) -> str:
    user = session.get(User, user_id)
    if user is None or not user.totp_secret_enc:
        raise FinPilotError("POLICY_DENIED", "该用户尚未配置强验证因子。")
    return decrypt_secret(user.totp_secret_enc)


def get_user(session: Session, user_id: str) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该对象。")
    return user


def auth_context_for_system(user_id: str, *, channel: str = "system") -> AuthContext:
    """系统内部路径（工作者、对账、结算）使用的可信上下文。

    只供服务端后台模块调用；外部请求不得构造该上下文。
    """
    return AuthContext(user_id=user_id, roles=("system", "customer"), channel=channel, session_id="system")