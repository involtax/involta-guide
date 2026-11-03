from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from involta_guide.infrastructure.security import ADMIN_SESSION_TTL, Security


def security_with_session(expires_at, *, revoked=False):
    session = {"id": "test-session", "expires_at": expires_at, "revoked": revoked}
    repo = MagicMock()
    repo.rows.return_value = [session]
    repo.get.return_value = session
    security = Security.__new__(Security)
    security.uow = MagicMock()
    security.uow.return_value.__enter__.return_value.repo = repo
    return security, repo


def test_active_admin_session_extends_expiry_and_requests_cookie_renewal():
    before = datetime.now(UTC)
    security, repo = security_with_session(before + timedelta(hours=1))
    assert security.renew_admin("test-token")
    table, key, changes = repo.change.call_args.args
    assert (table, key) == ("admin_sessions", "test-session")
    assert before + ADMIN_SESSION_TTL <= changes["expires_at"]
    assert changes["expires_at"] <= datetime.now(UTC) + ADMIN_SESSION_TTL
    assert "revoked" not in changes


def test_recent_session_does_not_write_or_refresh_cookie_again():
    security, repo = security_with_session(datetime.now(UTC) + ADMIN_SESSION_TTL)
    assert not security.renew_admin("test-token")
    repo.change.assert_not_called()


@pytest.mark.parametrize("revoked", [False, True])
def test_expired_or_revoked_session_is_not_renewed(revoked):
    expiry = datetime.now(UTC) + timedelta(days=1) if revoked else datetime.now(UTC)
    security, repo = security_with_session(expiry, revoked=revoked)
    assert not security.renew_admin("test-token")
    repo.change.assert_not_called()


def test_revocation_between_lookup_and_lock_is_respected():
    security, repo = security_with_session(datetime.now(UTC) + timedelta(hours=1))
    repo.get.return_value = {**repo.get.return_value, "revoked": True}
    assert not security.renew_admin("test-token")
    repo.change.assert_not_called()
