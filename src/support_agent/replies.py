"""Deterministic pt-BR wording of tool outputs, and the grounding check on LLM replies.

The facts rendered here are the only source the responder may draw on. A reply that
mentions a date, amount or status absent from the facts is discarded in favour of the
facts themselves.
"""

import re
from datetime import date, datetime

from support_agent.logistics import ShipmentStatus
from support_agent.models import OrderStatus
from support_agent.tools import ErrorCode, OrderStatusResult, ToolError

ASK_ORDER_ID = (
    "Claro, posso verificar! Qual é o número do pedido? Você encontra no e-mail de "
    "confirmação da compra (por exemplo, ORD-1002)."
)
CAPABILITIES = (
    "Por enquanto consigo te ajudar a acompanhar seus pedidos: me diga o número do pedido "
    "e eu verifico o status e a entrega."
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
    ShipmentStatus.RETURNED: "devolvido ao remetente",
}


def _d(value: date | datetime) -> str:
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


def render_order_status(result: OrderStatusResult) -> str:
    lines = [f"Pedido {result.order_id}: {ORDER_STATUS_LABELS[result.status]}."]
    shipment = result.shipment

    if result.status is OrderStatus.CANCELLED:
        return lines[0]
    if result.delivered_at is not None:
        lines.append(f"Entregue em {_d(result.delivered_at)}.")
    elif shipment is None:
        lines.append("Ele ainda não foi enviado à transportadora.")

    if result.promised_delivery_date is not None:
        lines.append(f"Previsão de entrega: {_d(result.promised_delivery_date)}.")
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
                f"Última atualização em {_d(event.at)}{where}: {event.description}."
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
_STATUS_WORDS = re.compile(
    "|".join(
        rf"\b{re.escape(label)}"
        for label in {*ORDER_STATUS_LABELS.values(), *SHIPMENT_STATUS_LABELS.values()}
    ),
    re.IGNORECASE,
)


def is_grounded(reply: str, facts: str) -> bool:
    """True if every date, amount and status the reply mentions also appears in the facts."""
    folded_facts = facts.casefold()
    return all(
        token.casefold() in folded_facts
        for pattern in (_DATE, _AMOUNT, _STATUS_WORDS)
        for token in pattern.findall(reply)
    )
