"""Why an LLM reply is discarded for the facts: the grounded and complete checks."""

from support_agent.replies import Facts, FallbackReason, fallback_reason, grounded_reply

FACTS = Facts(
    text=(
        "Pedido ORD-1002: enviado.\nPrevisão de entrega: 03/09/2026.\n"
        "Transportadora: Correios, código de rastreio BR100000002."
    ),
    statuses=frozenset({"enviado"}),
    required=frozenset({frozenset({"BR100000002"}), frozenset({"03/09/2026"})}),
)
COMPLETE = "Seu pedido foi enviado (rastreio BR100000002) e chega em 03/09/2026."


def test_a_grounded_complete_reply_has_no_fallback() -> None:
    assert fallback_reason(COMPLETE, FACTS) is None
    assert grounded_reply(COMPLETE, FACTS) == COMPLETE


def test_an_ungrounded_reply_names_the_tokens_beyond_the_facts() -> None:
    reply = f"{COMPLETE} Já foi entregue, R$ 10,00 de frete, em 5 dias."

    reason = fallback_reason(reply, FACTS)

    assert reason == FallbackReason(ungrounded=["entregue", "R$ 10,00", "5 dias"])
    assert reason.checks == ["grounded"]
    assert grounded_reply(reply, FACTS) == FACTS.text


def test_an_incomplete_reply_names_the_facts_it_leaves_out_or_negates() -> None:
    reply = "Seu pedido foi enviado, mas não chega em 03/09/2026."

    reason = fallback_reason(reply, FACTS)

    assert reason == FallbackReason(unaffirmed=[["03/09/2026"], ["BR100000002"]])
    assert reason.checks == ["complete"]
    assert grounded_reply(reply, FACTS) == FACTS.text


def test_a_reply_can_fail_both_checks() -> None:
    reply = "Seu pedido chega em 25/12/2030."

    reason = fallback_reason(reply, FACTS)

    assert reason == FallbackReason(
        ungrounded=["25/12/2030"], unaffirmed=[["03/09/2026"], ["BR100000002"]]
    )
    assert reason.checks == ["grounded", "complete"]


def test_an_empty_reply_falls_back() -> None:
    reason = fallback_reason("  ", Facts(text="Pedido ORD-1001: cancelado."))

    assert reason == FallbackReason(empty=True)
    assert reason.checks == ["empty"]


def test_a_status_named_only_to_deny_it_is_not_stated() -> None:
    facts = Facts(
        text="Pedido ORD-1003: em separação.\nPrevisão de entrega: 08/09/2026.",
        statuses=frozenset({"em separação"}),
    )
    reply = "Seu pedido está em separação e ainda não foi enviado à transportadora."

    assert fallback_reason(reply, facts) is None
