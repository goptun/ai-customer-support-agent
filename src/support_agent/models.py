import enum
import uuid
from datetime import date, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    BigInteger,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Output size of the embedding model; changing the model needs a migration.
EMBEDDING_DIM = 384


class Base(DeclarativeBase):
    pass


class OrderStatus(enum.StrEnum):
    PENDING_PAYMENT = "pending_payment"
    PAID = "paid"
    PROCESSING = "processing"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class MessageRole(enum.StrEnum):
    CUSTOMER = "customer"
    AGENT = "agent"


class TicketReason(enum.StrEnum):
    CUSTOMER_REQUESTED = "customer_requested"
    TOOL_FAILURE = "tool_failure"
    CLARIFICATION_FAILED = "clarification_failed"


class TicketPriority(enum.StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class ActorType(enum.StrEnum):
    CUSTOMER = "customer"
    ATTENDANT = "attendant"
    SYSTEM = "system"


def _enum(cls: type[enum.StrEnum], name: str) -> Enum:
    return Enum(cls, name=name, values_callable=lambda members: [m.value for m in members])


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320), unique=True)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    status: Mapped[OrderStatus] = mapped_column(_enum(OrderStatus, "order_status"))
    items: Mapped[list[dict[str, object]]] = mapped_column(JSON)
    total_paid_cents: Mapped[int] = mapped_column(BigInteger)
    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    promised_delivery_date: Mapped[date | None] = mapped_column(Date)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Shipment(Base):
    __tablename__ = "shipments"

    id: Mapped[int] = mapped_column(primary_key=True)
    # unique: an Order has at most one Shipment
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), unique=True)
    tracking_code: Mapped[str] = mapped_column(String(64), unique=True)
    carrier: Mapped[str] = mapped_column(String(100))
    dispatched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"), index=True)
    role: Mapped[MessageRole] = mapped_column(_enum(MessageRole, "message_role"))
    content: Mapped[str] = mapped_column(Text)
    intent: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # ticket:{conversation_id}:{reason}; a repeat returns the original Ticket
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"), index=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.id"))
    reason: Mapped[TicketReason] = mapped_column(_enum(TicketReason, "ticket_reason"))
    priority: Mapped[TicketPriority] = mapped_column(_enum(TicketPriority, "ticket_priority"))
    summary: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditLogEntry(Base):
    """Append-only (enforced by a trigger): one entry per side effect."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    actor_type: Mapped[ActorType] = mapped_column(_enum(ActorType, "actor_type"))
    actor_id: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64), index=True)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KnowledgeChunk(Base):
    """One section of a Knowledge Base policy document, with its embedding."""

    __tablename__ = "knowledge_chunks"
    __table_args__ = (UniqueConstraint("document", "position"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document: Mapped[str] = mapped_column(String(100), index=True)  # source file stem
    position: Mapped[int] = mapped_column(Integer)  # section order within the document
    title: Mapped[str] = mapped_column(String(200))
    section: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))  # sha256 of what was embedded
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
