"""Dev JWT auth: tokens only for seeded identities, with a role claim."""

import enum
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

ALGORITHM = "HS256"


class Role(enum.StrEnum):
    CUSTOMER = "customer"
    ATTENDANT = "attendant"


@dataclass(frozen=True)
class Principal:
    subject: str
    role: Role


class InvalidToken(Exception):
    pass


def issue_token(principal: Principal, secret: str, ttl_seconds: int) -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": principal.subject,
        "role": principal.role.value,
        "iat": now,
        "exp": now + timedelta(seconds=ttl_seconds),
    }
    return jwt.encode(claims, secret, algorithm=ALGORITHM)


def verify_token(token: str, secret: str) -> Principal:
    try:
        claims = jwt.decode(
            token, secret, algorithms=[ALGORITHM], options={"require": ["sub", "role", "exp"]}
        )
        return Principal(subject=claims["sub"], role=Role(claims["role"]))
    except (jwt.PyJWTError, ValueError) as exc:
        raise InvalidToken from exc
