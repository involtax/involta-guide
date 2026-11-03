import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError

from involta_guide.application.ports.repositories import Record, UowFactory
from involta_guide.config import Settings
from involta_guide.domain.entities import RuleViolation

ADMIN_SESSION_TTL = timedelta(days=365)
ADMIN_SESSION_MAX_AGE = int(ADMIN_SESSION_TTL.total_seconds())


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Security:
    def __init__(self, settings: Settings, uow: UowFactory) -> None:
        self.settings = settings
        self.uow = uow
        self.hasher = PasswordHasher()
        configured = settings.admin_password_hash.get_secret_value()
        password = settings.admin_password.get_secret_value()
        self.password_hash = configured or (
            self.hasher.hash(password) if len(password) >= 12 else ""
        )

    def fingerprint(self, ip: str) -> str:
        return hmac.new(
            self.settings.session_secret.get_secret_value().encode(), ip.encode(), hashlib.sha256
        ).hexdigest()

    def csrf(self, token: str) -> str:
        return self.fingerprint("csrf:" + token)

    def principal(self, token: str | None, *, alex: bool = False) -> Record | None:
        if not token or len(token) > 200:
            return None
        with self.uow() as uow:
            rows = uow.repo.rows(
                "alex_sessions" if alex else "admin_sessions",
                {"token_hash": digest(token)},
                limit=1,
            )
            if rows and not rows[0]["revoked"] and rows[0]["expires_at"] > datetime.now(UTC):
                return rows[0]
        return None

    def login(self, password: str, ip_hash: str) -> str:
        # Rate attempt is committed even when password verification fails.
        with self.uow() as uow:
            uow.repo.rate_limit("login:" + ip_hash, 10, 900)
        if not self.password_hash:
            raise RuleViolation("Настройте ADMIN_PASSWORD_HASH или ADMIN_PASSWORD от 12 символов")
        try:
            self.hasher.verify(self.password_hash, password)
        except VerificationError as exc:
            raise RuleViolation("Неверный пароль") from exc
        token = secrets.token_hex(32)
        with self.uow() as uow:
            uow.repo.add(
                "admin_sessions",
                {
                    "id": str(uuid4()),
                    "token_hash": digest(token),
                    "csrf_hash": digest(self.csrf(token)),
                    "expires_at": datetime.now(UTC) + ADMIN_SESSION_TTL,
                },
            )
            uow.repo.audit("admin_login", "admin")
        return token

    def renew_admin(self, token: str) -> bool:
        session = self.principal(token)
        now = datetime.now(UTC)
        if session is None or session["expires_at"] > now + ADMIN_SESSION_TTL - timedelta(days=1):
            return False
        with self.uow() as uow:
            # Recheck under a lock so renewal cannot revive an expired or revoked session.
            current = uow.repo.get("admin_sessions", session["id"], lock=True)
            now = datetime.now(UTC)
            if current is None or current["revoked"] or current["expires_at"] <= now:
                return False
            if current["expires_at"] > now + ADMIN_SESSION_TTL - timedelta(days=1):
                return False
            uow.repo.change(
                "admin_sessions", current["id"], {"expires_at": now + ADMIN_SESSION_TTL}
            )
        return True

    def alex_login(self, secret: str) -> str | None:
        expected = self.settings.alex_secret.get_secret_value()
        if not expected or not hmac.compare_digest(
            secret.encode("utf-8"), expected.encode("utf-8")
        ):
            return None
        token = secrets.token_hex(32)
        with self.uow() as uow:
            key = str(uuid4())
            uow.repo.add(
                "alex_sessions",
                {
                    "id": key,
                    "token_hash": digest(token),
                    "expires_at": datetime.now(UTC) + timedelta(days=30),
                },
            )
            uow.repo.audit("alex_login", key)
        return token

    def revoke(self, token: str, *, alex: bool = False, all_sessions: bool = False) -> None:
        table = "alex_sessions" if alex else "admin_sessions"
        with self.uow() as uow:
            rows = (
                uow.repo.rows(table, {"revoked": False}, limit=10000)
                if all_sessions
                else uow.repo.rows(table, {"token_hash": digest(token)})
            )
            for row in rows:
                uow.repo.change(table, row["id"], {"revoked": True})
            uow.repo.audit("sessions_revoked", table)
