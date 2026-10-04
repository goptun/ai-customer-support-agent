"""Deterministic pt-BR wording of tool outputs, and the grounding check on LLM replies.

The facts rendered here are the only source the responder may draw on. A reply that
mentions a date, amount or day count absent from the facts, or a status the facts don't
state, is discarded in favour of the facts themselves; so is one that leaves out a fact the
Customer needs (an Order's tracking code or its relevant date).
"""

import re
from dataclasses import dataclass
from datetime import date, datetime

from support_agent.cancellations import CancellationResult
from support_agent.llm import Intent
from support_agent.logistics import ShipmentStatus
from support_agent.models import OrderStatus, RefundStatus, TicketReason
from support_agent.orders import OrderSummary
from support_agent.refunds import RefundRefusal, RefundRequested
from support_agent.tickets import TicketResult
from support_agent.tools import (
    CancellationPreview,
    ErrorCode,
    KnowledgeBaseResult,
    OrderStatusResult,
    RefundRefused,
    RefundStatusResult,
    ToolError,
)

ASK_ORDER_ID = (
    "Claro, posso verificar! Qual é o número do pedido? Você encontra no e-mail de "
    "confirmação da compra (por exemplo, ORD-1002)."
)
CAPABILITIES = (
    "Sou o assistente de atendimento da loja. Por enquanto consigo te ajudar a acompanhar "
    "seus pedidos (me diga o número do pedido e eu verifico o status e a entrega), cancelar "
    "pedidos que ainda não foram enviados, solicitar e acompanhar reembolsos e tirar dúvidas "
    "sobre as políticas da loja, como "
    "prazos de entrega, trocas e reembolsos. Se preferir, também posso te passar para um "
    "atendente."
)
CLARIFICATION = (
    "Não tenho certeza se entendi o que você precisa. Pode me explicar com outras palavras? "
    "Por exemplo: saber onde está um pedido ou tirar uma dúvida sobre trocas."
)
# Said instead of guessing when the Knowledge Base doesn't cover the question.
KNOWLEDGE_NOT_FOUND = (
    "Não encontrei essa informação nas políticas da loja, então prefiro não te dar uma "
    "resposta incerta. Se quiser, posso abrir um chamado para um atendente te responder: "
    "é só me pedir."
)

# How a queued Intent is announced: "Também vi que você quer {label}."
INTENT_LABELS = {
    Intent.ORDER_STATUS: "saber o status do pedido",
    Intent.CANCEL_ORDER: "cancelar um pedido",
    Intent.REFUND: "um reembolso",
    Intent.FAQ: "tirar uma dúvida sobre as políticas da loja",
    Intent.OTHER: "outra coisa",
}

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


def _format_money(cents: int) -> str:
    reais, centavos = divmod(cents, 100)
    return f"R$ {reais:,}".replace(",", ".") + f",{centavos:02d}"


def render_error(error: ToolError) -> str:
    if error.code in (ErrorCode.NOT_FOUND, ErrorCode.NOT_OWNED):
        # Identical wording for both: never reveal that someone else's Order exists.
        return (
            f"Não encontrei o pedido {error.order_id} entre os seus pedidos. "
            "Confere o número e me manda de novo?"
        )
    if error.code is ErrorCode.INVALID_ARGUMENT:
        return "Não reconheci o número do pedido. " + ASK_ORDER_ID
    if error.code is ErrorCode.ALREADY_DISPATCHED:
        return (
            f"O pedido {error.order_id} já foi enviado à transportadora, então não pode mais ser "
            "cancelado. Você pode recusar a entrega quando ela chegar ou, depois de receber, "
            "solicitar a devolução (Política de Cancelamento; Política de Devolução e Direito "
            "de Arrependimento)."
        )
    if error.code is ErrorCode.ALREADY_CANCELLED:
        return f"O pedido {error.order_id} já está cancelado, não há mais nada a fazer nele."
    return "Não consegui consultar seu pedido agora. Tente novamente em alguns minutos."


def render_ticket(ticket: TicketResult) -> str:
    next_steps = (
        f"Abri o chamado #{ticket.ticket_id} com o resumo da nossa conversa, então você não "
        "vai precisar repetir nada. Um atendente vai continuar o atendimento por aqui assim "
        "que possível."
    )
    if ticket.reason is TicketReason.CUSTOMER_REQUESTED:
        return f"Combinado, vou te passar para um atendente. {next_steps}"
    if ticket.reason is TicketReason.CLARIFICATION_FAILED:
        return (
            "Desculpe, não consegui entender o que você precisa, então vou te passar para um "
            f"atendente. {next_steps}"
        )
    return (
        "Nossos sistemas estão instáveis e não consegui concluir a consulta, então vou te "
        f"passar para um atendente. {next_steps}"
    )


def render_cancellation_question(preview: CancellationPreview) -> str:
    if preview.refund_cents > 0:
        refund = (
            f"Como ele já está pago, o reembolso integral de {_format_money(preview.refund_cents)} "
            "será feito automaticamente."
        )
    else:
        refund = "Como não há pagamento a devolver, não há valor a reembolsar."
    return (
        f"Você quer mesmo cancelar o pedido {preview.order_id} inteiro? {refund} "
        'Responda "sim" para confirmar o cancelamento ou "não" para manter o pedido.'
    )


def render_cancelled(result: CancellationResult) -> str:
    done = f"Pronto, cancelei o pedido {result.order_id}."
    if result.refund is None:
        return done
    return (
        f"{done} O reembolso de {_format_money(result.refund.amount_cents)} foi emitido "
        f"(reembolso #{result.refund.refund_id})."
    )


def render_cancellation_kept(order_id: str) -> str:
    return f"Tudo bem, não cancelei nada: o pedido {order_id} continua ativo."


def render_cancellation_dropped(order_id: str) -> str:
    return f"Como você não confirmou, não cancelei o pedido {order_id}."


def render_refund_changed(order_id: str) -> str:
    return (
        f"O pedido {order_id} mudou desde a minha pergunta e o reembolso não seria mais o que "
        "você confirmou, então não cancelei nada."
    )


REFUND_STATUS_LABELS = {
    RefundStatus.PENDING_APPROVAL: "aguardando aprovação de um atendente",
    RefundStatus.APPROVED: "aprovado, aguardando emissão",
    RefundStatus.ISSUED: "emitido",
    RefundStatus.REJECTED: "recusado",
}
# Said with every refusal: the Customer can still contest it with a human.
TICKET_OFFER = (
    "Se quiser contestar, posso abrir um chamado para um atendente analisar o seu caso: "
    "é só me pedir."
)


def render_refund_requested(result: RefundRequested) -> str:
    awaiting = (
        "Pela Política de Reembolso, todo reembolso é aprovado por um atendente antes de ser "
        f"emitido, então o reembolso #{result.refund_id} está aguardando aprovação. Você pode "
        "continuar falando comigo enquanto isso e me perguntar o status do reembolso quando "
        "quiser."
    )
    amount = _format_money(result.amount_cents)
    if result.already_pending:
        return (
            f"O pedido {result.order_id} já tem um reembolso de {amount} aguardando aprovação "
            f"de um atendente (reembolso #{result.refund_id}), então não abri outro nem mudei "
            "o valor dele. Você pode me perguntar o status do reembolso quando quiser; se "
            "precisar de outro valor, posso abrir um chamado para um atendente."
        )
    maximum = _format_money(result.max_cents)
    if result.capped and result.requested_cents is not None:
        what = (
            f"Você pediu {_format_money(result.requested_cents)}, mas o máximo que pode ser "
            f"reembolsado neste pedido é {maximum} (o valor pago menos reembolsos anteriores), "
            f"então solicitei o reembolso de {amount}."
        )
    elif result.amount_cents < result.max_cents:
        what = f"Solicitei o reembolso de {amount}, como você pediu (o máximo seria {maximum})."
    else:
        what = f"Solicitei o reembolso de {amount}, o valor máximo para este pedido."
    return f"O pedido {result.order_id} tem direito a reembolso. {what} {awaiting}"


def render_refund_decision(
    order_id: str, refund_id: int, amount_cents: int, approved: bool, reason: str | None
) -> str:
    """The Attendant's decision, as relayed to the Customer."""
    refund = f"O reembolso #{refund_id} de {_format_money(amount_cents)} do pedido {order_id}"
    if approved:
        return f"{refund} foi aprovado por um atendente e emitido."
    because = (reason or "").rstrip(".")
    return f"{refund} foi recusado por um atendente. Motivo: {because}. {TICKET_OFFER}"


def render_refund_refused(result: RefundRefused) -> str:
    order_id = result.order_id
    why = {
        RefundRefusal.ORDER_CANCELLED: (
            f"O pedido {order_id} está cancelado: se ele estava pago, o reembolso integral já "
            "foi emitido no cancelamento (Política de Cancelamento)."
        ),
        RefundRefusal.NOT_DISPATCHED: (
            f"O pedido {order_id} ainda não foi enviado, então não cabe reembolso: você pode "
            "cancelá-lo, e se ele já estiver pago o reembolso integral é feito automaticamente "
            "(Política de Cancelamento)."
        ),
        RefundRefusal.DELIVERY_WINDOW_EXPIRED: (
            f"O pedido {order_id} foi entregue há mais de {result.delivery_window_days} dias, e "
            "pela Política de Reembolso só é possível pedir reembolso de pedidos entregues há "
            f"no máximo {result.delivery_window_days} dias."
        ),
        RefundRefusal.NOT_LATE_ENOUGH: (
            f"O pedido {order_id} ainda está a caminho, sem extravio informado pela "
            f"transportadora e sem passar de {result.late_after_days} dias de atraso em relação "
            "à previsão de entrega. Pela Política de Reembolso e pela Política de Atrasos e "
            "Extravios, o reembolso de um pedido a caminho só é possível nesses casos."
        ),
        RefundRefusal.NOTHING_LEFT: (
            f"Os reembolsos do pedido {order_id} já somam o valor pago nele, então não há mais "
            "valor a reembolsar (Política de Reembolso)."
        ),
        # Reported as a carrier failure, never as a refusal; listed to keep this exhaustive.
        RefundRefusal.CARRIER_UNAVAILABLE: (
            "Não consegui consultar a transportadora agora. Tente novamente em alguns minutos."
        ),
    }[result.reason]
    return f"{why} {TICKET_OFFER}"


def render_refund_status(result: RefundStatusResult) -> str:
    whose = f"do pedido {result.order_id}" if result.order_id else "seus"
    if not result.refunds:
        return f"Não encontrei reembolsos {whose}."
    lines = [
        f"- Reembolso #{refund.refund_id} do pedido {refund.order_id}, de "
        f"{_format_money(refund.amount_cents)}: {REFUND_STATUS_LABELS[refund.status]}."
        for refund in result.refunds
    ]
    heading = f"Reembolsos do pedido {result.order_id}:" if result.order_id else "Seus reembolsos:"
    return "\n".join([heading, *lines])


def render_recent_orders(orders: list[OrderSummary]) -> str:
    lines = [
        f"- {order.order_id}, feito em {_format_date(order.placed_at)}: "
        f"{ORDER_STATUS_LABELS[order.status]}"
        for order in orders
    ]
    return "\n".join(
        [
            "Encontrei estes pedidos recentes seus:",
            *lines,
            "Qual deles você quer consultar? Se for outro, me diga o número do pedido.",
        ]
    )


def render_single_recent_order(order_id: str) -> str:
    return f"Encontrei um pedido recente seu, o {order_id}."


def render_queued(intents: list[Intent]) -> str:
    wants = " e ".join(INTENT_LABELS[intent] for intent in intents)
    return f"Também vi que você quer {wants}. É só me responder que eu sigo com isso em seguida."


@dataclass(frozen=True)
class Facts:
    text: str
    # Status labels the Order is actually in; compared structurally, since the text may
    # mention a status only to negate it ("ainda não foi enviado").
    statuses: frozenset[str] = frozenset()
    # Literals the reply must repeat: without them it doesn't answer the Customer.
    required: frozenset[str] = frozenset()


def render_order_status(result: OrderStatusResult) -> Facts:
    return Facts(
        text=_order_status_text(result),
        statuses=_current_statuses(result),
        required=_required_facts(result),
    )


def render_knowledge(result: KnowledgeBaseResult) -> Facts:
    text = "\n\n".join(
        f"{chunk.title} — {chunk.section}:\n{chunk.content}" for chunk in result.chunks
    )
    # A policy may mention any status ("pedido entregue há 30 dias"): all of them are facts.
    return Facts(text=text, statuses=frozenset(_STATUS_WORDS.findall(text)))


def cite_sources(reply: str, result: KnowledgeBaseResult) -> str:
    """The reply, naming the policies it comes from if it doesn't already."""
    titles = list(dict.fromkeys(chunk.title for chunk in result.chunks))
    if any(title.casefold() in reply.casefold() for title in titles):
        return reply
    label = "Fonte" if len(titles) == 1 else "Fontes"
    return f"{reply}\n\n{label}: {'; '.join(titles)}."


def _current_statuses(result: OrderStatusResult) -> frozenset[str]:
    statuses = {ORDER_STATUS_LABELS[result.status]}
    if result.shipment is not None and result.shipment.tracking is not None:
        statuses.add(SHIPMENT_STATUS_LABELS[result.shipment.tracking.status])
    if result.days_late:
        statuses.add(SHIPMENT_STATUS_LABELS[ShipmentStatus.DELAYED])
    return frozenset(statuses)


def _required_facts(result: OrderStatusResult) -> frozenset[str]:
    if result.status is OrderStatus.CANCELLED:
        return frozenset()
    if result.delivered_at is not None:
        return frozenset({_format_date(result.delivered_at)})
    required = set()
    if result.promised_delivery_date is not None:
        required.add(_format_date(result.promised_delivery_date))
    if result.shipment is not None:
        required.add(result.shipment.tracking_code)
    return frozenset(required)


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


def is_complete(reply: str, facts: Facts) -> bool:
    """True if the reply repeats every fact the Customer needs."""
    folded_reply = reply.casefold()
    return all(fact.casefold() in folded_reply for fact in facts.required)
