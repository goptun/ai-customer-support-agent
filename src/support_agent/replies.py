"""Deterministic pt-BR wording of tool outputs, and the grounding check on LLM replies.

The facts rendered here are the only source the responder may draw on. A reply that
mentions a date, amount or day count absent from the facts, or a status the Order is not
in, is discarded in favour of the facts themselves.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime

from support_agent.logistics import ShipmentStatus
from support_agent.models import OrderStatus, TicketReason
from support_agent.tickets import TicketResult
from support_agent.tools import ErrorCode, OrderStatusResult, ToolError

ASK_ORDER_ID = (
    "Claro, posso verificar! Qual é o número do pedido? Você encontra no e-mail de "
    "confirmação da compra (por exemplo, ORD-1002)."
)
CAPABILITIES = (
    "Por enquanto consigo te ajudar a acompanhar seus pedidos: me diga o número do pedido "
    "e eu verifico o status e a entrega."
)

# Escalation must never leave the Customer without a way forward, even if the Ticket fails.
TICKET_FAILED = (
    "Não consegui abrir um chamado para um atendente agora. Por favor, tente novamente em "
    "alguns minutos."
)

ORDER_STATUS_LABELS = {
    OrderStatus.PENDING_PAYMENT: "aguardando pagamento",
    OrderStatus.PAID: "pagamento confirmado",
    OrderStatus.PROCESSING: "em separação",
    OrderStatus.SHIPPED: "enviado",
    OrderStatus.DELIVERED: "entregue",
    OrderStatus.CANCELLED: "cancelado",
}
SHIPMENT_STATUS_LABELS = {
    ShipmentStatus.IN_TRANSIT: "em trânsito",
    ShipmentStatus.DELAYED: "atrasado",
    ShipmentStatus.DELIVERED: "entregue",
    ShipmentStatus.LOST: "extraviado",
    ShipmentStatus.RETURNED: "devolvido",
}


def _format_date(value: date | datetime) -> str:
    return value.strftime("%d/%m/%Y")


def render_error(error: ToolError) -> str:
    if error.code in (ErrorCode.NOT_FOUND, ErrorCode.NOT_OWNED):
        # Identical wording for both: never reveal that someone else's Order exists.
        return (
            f"Não encontrei o pedido {error.order_id} entre os seus pedidos. "
            "Confere o número e me manda de novo?"
        )
    if error.code is ErrorCode.INVALID_ARGUMENT:
        return "Não reconheci o número do pedido. " + ASK_ORDER_ID
    return "Não consegui consultar seu pedido agora. Tente novamente em alguns minutos."


def render_ticket(ticket: TicketResult) -> str:
    next_steps = (
        f"Abri o chamado #{ticket.ticket_id} com o resumo da nossa conversa, então você não "
        "vai precisar repetir nada. Um atendente vai continuar o atendimento por aqui assim "
        "que possível."
    )
    if ticket.reason is TicketReason.CUSTOMER_REQUESTED:
        return f"Combinado, vou te passar para um atendente. {next_steps}"
    return (
        "Nossos sistemas estão instáveis e não consegui concluir a consulta, então vou te "
        f"passar para um atendente. {next_steps}"
    )


@dataclass(frozen=True)
class Facts:
    text: str
    # Status labels the Order is actually in; compared structurally, since the text may
    # mention a status only to negate it ("ainda não foi enviado").
    statuses: frozenset[str] = frozenset()


def render_order_status(result: OrderStatusResult) -> Facts:
    return Facts(text=_order_status_text(result), statuses=_current_statuses(result))


def _current_statuses(result: OrderStatusResult) -> frozenset[str]:
    statuses = {ORDER_STATUS_LABELS[result.status]}
    if result.shipment is not None and result.shipment.tracking is not None:
        statuses.add(SHIPMENT_STATUS_LABELS[result.shipment.tracking.status])
    if result.days_late:
        statuses.add(SHIPMENT_STATUS_LABELS[ShipmentStatus.DELAYED])
    return frozenset(statuses)


def _order_status_text(result: OrderStatusResult) -> str:
    lines = [f"Pedido {result.order_id}: {ORDER_STATUS_LABELS[result.status]}."]
    shipment = result.shipment

    if result.status is OrderStatus.CANCELLED:
        return lines[0]
    if result.delivered_at is not None:
        lines.append(f"Entregue em {_format_date(result.delivered_at)}.")
    elif shipment is None:
        lines.append("Ele ainda não foi enviado à transportadora.")

    if result.promised_delivery_date is not None:
        lines.append(f"Previsão de entrega: {_format_date(result.promised_delivery_date)}.")
    if result.days_late:
        verb = "foi entregue" if result.delivered_at is not None else "está"
        lines.append(f"O pedido {verb} {result.days_late} dias atrasado em relação à previsão.")

    if shipment is not None:
        lines.append(
            f"Transportadora: {shipment.carrier}, código de rastreio {shipment.tracking_code}."
        )
        if shipment.tracking is not None:
            event = shipment.tracking.latest_event
            where = f" ({event.location})" if event.location else ""
            lines.append(
                f"Situação na transportadora: {SHIPMENT_STATUS_LABELS[shipment.tracking.status]}. "
                f"Última atualização em {_format_date(event.at)}{where}: {event.description}."
            )
        elif shipment.tracking_error is ErrorCode.TRACKING_NOT_FOUND:
            lines.append("A transportadora ainda não tem informações de rastreio para este código.")
        else:
            lines.append(
                "Não consegui consultar a transportadora agora, então não tenho a atualização "
                "mais recente do rastreio. Tente novamente em alguns minutos."
            )
    return "\n".join(lines)


_DATE = re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b|\b\d{4}-\d{2}-\d{2}\b")
_AMOUNT = re.compile(r"R\$\s?\d[\d.]*(?:,\d{1,2})?")
_DAYS = re.compile(r"\b\d+\s+dias?\b", re.IGNORECASE)
_STATUS_WORDS = re.compile(
    "|".join(
        rf"\b{re.escape(label)}\b"
        for label in {*ORDER_STATUS_LABELS.values(), *SHIPMENT_STATUS_LABELS.values()}
    ),
    re.IGNORECASE,
)


def is_grounded(reply: str, facts: Facts) -> bool:
    """True if the reply states no date, amount, day count or status beyond the facts."""
    folded_text = facts.text.casefold()
    folded_statuses = {status.casefold() for status in facts.statuses}
    literals_grounded = all(
        token.casefold() in folded_text
        for pattern in (_DATE, _AMOUNT, _DAYS)
        for token in pattern.findall(reply)
    )
    statuses_grounded = all(
        word.casefold() in folded_statuses for word in _STATUS_WORDS.findall(reply)
    )
    return literals_grounded and statuses_grounded
