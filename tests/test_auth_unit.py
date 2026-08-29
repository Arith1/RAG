"""认证工具单元测试：bcrypt 密码哈希 / JWT 签发与校验。"""
import asyncio
import jwt
import pytest
from unittest.mock import AsyncMock

from rag个人知识库.api.auth import (
    JWT_ALGORITHM, JWT_SECRET, create_access_token, get_current_user, hash_password,
    verify_password,
)
from rag个人知识库.models.user import User


class TestPassword:
    def test_hash_and_verify(self):
        h = hash_password("secret123")
        assert h != "secret123"
        assert verify_password("secret123", h)
        assert not verify_password("wrong", h)

    def test_salt_randomness(self):
        assert hash_password("same") != hash_password("same")


class TestJWT:
    def _user(self, **kw):
        defaults = dict(id=1, username="admin", role="admin")
        defaults.update(kw)
        return User(**defaults)

    def test_token_roundtrip(self):
        token = create_access_token(self._user())
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        assert payload["sub"] == "1"
        assert payload["role"] == "admin"
        assert "exp" in payload

    def test_token_has_expiry(self):
        token = create_access_token(self._user())
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        import time as _t
        assert payload["exp"] > _t.time()  # 未过期

    def test_tampered_token_rejected(self):
        token = create_access_token(self._user())
        tampered = token[:-2] + ("ab" if token[-2:] != "ab" else "cd")
        with pytest.raises(jwt.PyJWTError):
            jwt.decode(tampered, JWT_SECRET, algorithms=[JWT_ALGORITHM])


class TestGetCurrentUser:
    class _FakeResult:
        def __init__(self, user):
            self.user = user

        def scalar_one_or_none(self):
            return self.user

    def _run(self, user):
        token = create_access_token(user)

        async def _call():
            db = AsyncMock()
            db.execute.return_value = self._FakeResult(user)
            return await get_current_user(token=token, db=db)

        return asyncio.run(_call())

    def test_active_user_ok(self):
        user = User(id=1, username="u", role="user", status="active")
        assert self._run(user).id == 1

    def test_deleting_user_rejected(self):
        user = User(id=1, username="u", role="user", status="deleting")
        with pytest.raises(Exception) as exc:
            self._run(user)
        assert exc.value.status_code == 403

    def test_deleted_user_unauthorized(self):
        async def _call():
            db = AsyncMock()
            db.execute.return_value = self._FakeResult(None)
            return await get_current_user(token=create_access_token(User(id=9, username="gone", role="user")), db=db)

        with pytest.raises(Exception) as exc:
            asyncio.run(_call())
        assert exc.value.status_code == 401
