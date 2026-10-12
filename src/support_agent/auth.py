"""JWT auth: a role claim, and for a Demo Visitor the paired Attendant (ADR-0005)."""

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
    # A Demo Visitor's Customer principal carries its paired Attendant.
    attendant_id: str | None = None


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
    if principal.attendant_id is not None:
        claims["attendant"] = principal.attendant_id
    return jwt.encode(claims, secret, algorithm=ALGORITHM)


def verify_token(token: str, secret: str) -> Principal:
    try:
        claims = jwt.decode(
            token, secret, algorithms=[ALGORITHM], options={"require": ["sub", "role", "exp"]}
        )
        return Principal(
            subject=claims["sub"], role=Role(claims["role"]), attendant_id=claims.get("attendant")
        )
    except (jwt.PyJWTError, ValueError) as exc:
        raise InvalidToken from exc
