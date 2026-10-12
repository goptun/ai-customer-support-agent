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

    @property
    def is_demo_visitor(self) -> bool:
        return self.attendant_id is not None


class InvalidToken(Exception):
    pass


class ExpiredToken(InvalidToken):
    """Signed by us but past its expiry; carries whose it was."""

    def __init__(self, principal: Principal) -> None:
        self.principal = principal


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
    return _decode(token, secret, verify_exp=True)


def _decode(token: str, secret: str, *, verify_exp: bool) -> Principal:
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],
            options={"require": ["sub", "role", "exp"], "verify_exp": verify_exp},
        )
        return Principal(
            subject=claims["sub"], role=Role(claims["role"]), attendant_id=claims.get("attendant")
        )
    except jwt.ExpiredSignatureError as exc:
        # The signature checked out before the expiry did.
        raise ExpiredToken(_decode(token, secret, verify_exp=False)) from exc
    except (jwt.PyJWTError, ValueError) as exc:
        raise InvalidToken from exc
