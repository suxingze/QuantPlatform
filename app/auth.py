from __future__ import annotations

import time
from typing import Any

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .db import dict_row, password_hash

SECRET_KEY = "dev-secret-change-me"
ALGORITHM = "HS256"
TOKEN_TTL_SECONDS = 8 * 3600
bearer = HTTPBearer()


def create_token(user: dict[str, Any]) -> str:
    now = int(time.time())
    payload = {
        "sub": str(user["id"]),
        "username": user["username"],
        "role": user["role"],
        "iat": now,
        "exp": now + TOKEN_TTL_SECONDS,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def authenticate(conn, username: str, password: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    user = dict_row(row)
    if not user or user["password_hash"] != password_hash(password):
        return None
    return user


def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(bearer)) -> dict[str, Any]:
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="invalid token") from exc
    return {
        "id": int(payload["sub"]),
        "username": payload["username"],
        "role": payload["role"],
    }
