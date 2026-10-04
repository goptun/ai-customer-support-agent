"""Evaluation harness: scripted Conversations run against the app, scored into a report.

The real LLM and judge only run from the command line; here the app runs on the scripted
fake LLM and a fake judge, which is enough to check what the harness observes and scores.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from support_agent.db import session_factory
from support_agent.evaluation.dataset import Dataset, load_dataset, parse_dataset
from support_agent.evaluation.judge import JudgedTurn, Verdict
from support_agent.evaluation.markdown import render_markdown
from support_agent.evaluation.report import ModelIds, evaluate, percentile
from support_agent.evaluation.seeded import reset
from support_agent.llm import Classification, Confirmation, Entities, Intent
from support_agent.seed import CUSTOMERS

from .fakes import ScriptedLLM, cancel_order, order_status, refund

DATASETS = Path(__file__).parents[1] / "eval" / "datasets"
ORDER_STATUS_DATASET = DATASETS / "order_status.yaml"
MODELS = ModelIds(
    provider="fake-provider",
    classifier="fake-classifier",
    responder="fake-responder",
    judge="fake-judge",
)
STARTED_AT = datetime(2026, 10, 4, 15, 30, tzinfo=UTC)

DATASET = """
name: harness
conversations:
  - id: in_transit
    customer_id: cus_ana
    outcome: resolved
    turns:
      - message: "[eval] cadê o 1002?"
        intent: order_status
        tool_calls:
          - name: get_order_status
            args: {order_id: ORD-1002}
        facts: ["03/09/2026", ["BR100000002", "código de rastreio"]]
  - id: carrier_timeout
    customer_id: cus_carla
    outcome: escalated_appropriate
    turns:
      - message: "[eval] e o 3003?"
        intent: order_status
        tool_calls: [{name: get_order_status, args: {order_id: ORD-3003}}]
      - message: "[eval] tenta de novo o 3003"
        intent: order_status
        tool_calls:
          - {name: get_order_status, args: {order_id: ORD-3003}}
          - {name: create_support_ticket, args: {reason: tool_failure}}
  - id: needless_human
    customer_id: cus_diego
    outcome: resolved
    turns:
      - message: "[eval] quero falar com uma pessoa"
        intent: other
        tool_calls: []
  - id: which_order
    customer_id: cus_ana
    outcome: clarification
    turns:
      - message: "[eval] meu pedido não chegou"
        intent: order_status
        facts: ["ORD-1002", "não está aqui"]
"""


class FlagsReplies:
    """Fake judge: a reply mentioning `marker` is a hallucination."""

    def __init__(self, marker: str) -> None:
        self.marker = marker
        self.judged: list[JudgedTurn] = []

    async def judge(self, turn: JudgedTurn) -> Verdict:
        self.judged.append(turn)
        if self.marker in turn.reply:
            return Verdict(hallucinated=True, unsupported_claims=[f"mentions {self.marker}"])
        return Verdict(hallucinated=False, unsupported_claims=[])


def script(llm: ScriptedLLM) -> None:
    llm.on("[eval] cadê o 1002?", order_status("1002"))
    llm.on("[eval] e o 3003?", order_status("3003"))
    llm.on("[eval] tenta de novo o 3003", order_status("3003"))
    llm.on(
        "[eval] quero falar com uma pessoa",
        Classification(
            intents=[Intent.OTHER], confidence=0.95, entities=Entities(wants_human=True)
        ),
    )
    llm.on("[eval] meu pedido não chegou", order_status(None))


async def test_report_scores_each_conversation_run_against_the_app(
    client: AsyncClient, llm: ScriptedLLM
) -> None:
    script(llm)
    judge = FlagsReplies("ORD-1002")

    report = await evaluate(
        client, parse_dataset(DATASET), judge, models=MODELS, started_at=STARTED_AT
    )

    results = {result.id: result for result in report.conversations}
    assert results["in_transit"].completed
    assert results["in_transit"].observed_outcome == "resolved"
    assert results["carrier_timeout"].completed
    assert results["carrier_timeout"].observed_outcome == "escalated"
    assert results["carrier_timeout"].escalation == "appropriate"
    assert not results["needless_human"].completed
    assert results["needless_human"].observed_outcome == "escalated"
    assert results["needless_human"].escalation == "avoidable"
    # Asked which Order instead of acting, but a required fact is missing from the reply.
    assert results["which_order"].observed_outcome == "clarification"
    assert not results["which_order"].completed
    assert results["which_order"].turns[0].missing_facts == ["não está aqui"]

    summary = report.summary
    assert summary.conversations == 4
    assert summary.task_completion_rate == 0.5
    assert summary.tool_call_accuracy == 4 / 5  # needless_human opened an unexpected Ticket
    assert summary.intent_accuracy == 1.0
    assert summary.escalation_rate_appropriate == 0.25
    assert summary.escalation_rate_avoidable == 0.25
    # Judged: the turns the LLM wrote from tool outputs. Ticket replies are templates, and
    # which_order called no tool.
    assert len(judge.judged) == 2
    assert summary.hallucination_rate == 0.5
    assert results["in_transit"].turns[0].verdict == Verdict(
        hallucinated=True, unsupported_claims=["mentions ORD-1002"]
    )
    [first_judged, *_] = judge.judged
    assert first_judged.tool_outputs[0]["name"] == "get_order_status"
    assert first_judged.tool_outputs[0]["output"]["order_id"] == "ORD-1002"

    assert summary.latency_ms.turn.count == 5
    nodes = summary.latency_ms.nodes
    assert nodes["classify_intent"].count == 5
    assert nodes["act"].count == 3
    assert nodes["act"].p95 >= nodes["act"].p50 > 0
    assert len(report.review_sample) == 1  # ~10% of 4, at least one

    markdown = render_markdown(report)
    assert "2026-10-04" in markdown
    for model in ("fake-provider", "fake-classifier", "fake-responder", "fake-judge"):
        assert model in markdown
    assert report.model_dump(mode="json")["started_at"] == "2026-10-04T15:30:00Z"


@pytest.mark.parametrize(
    ("values", "q", "expected"),
    [
        ([10.0], 0.95, 10.0),
        ([1.0, 2.0, 3.0, 4.0], 0.5, 2.5),
        ([4.0, 1.0, 3.0, 2.0], 0.95, 3.85),
    ],
)
def test_percentiles_interpolate_between_samples(
    values: list[float], q: float, expected: float
) -> None:
    assert percentile(values, q) == pytest.approx(expected)


def test_a_conversation_needs_a_known_outcome_and_turns() -> None:
    with pytest.raises(ValidationError):
        parse_dataset(
            "name: bad\nconversations:\n"
            "  - {id: x, customer_id: cus_ana, outcome: solved, turns: [{message: oi}]}\n"
        )
    with pytest.raises(ValidationError):
        parse_dataset("name: bad\nconversations:\n  - {id: x, customer_id: cus_ana, turns: []}\n")
    with pytest.raises(ValidationError):  # an Attendant can't reject without a reason
        parse_dataset(
            "name: bad\nconversations:\n  - {id: x, customer_id: cus_ana, outcome: resolved,"
            " turns: [{message: oi, attendant: {decision: reject}}]}\n"
        )


CANCELS_TWICE = """
name: isolation
conversations:
  - id: first
    customer_id: cus_bruno
    outcome: resolved
    turns:
      - message: "[reset] cancela o 2001"
        tool_calls: [{name: preview_cancellation, args: {order_id: ORD-2001}}]
      - message: "[reset] sim"
        tool_calls: [{name: cancel_order, args: {order_id: ORD-2001}}]
        facts: ["Pronto, cancelei o pedido ORD-2001"]
  - id: second
    customer_id: cus_bruno
    outcome: resolved
    turns:
      - message: "[reset] cancela o 2001"
        tool_calls: [{name: preview_cancellation, args: {order_id: ORD-2001}}]
      - message: "[reset] sim"
        tool_calls: [{name: cancel_order, args: {order_id: ORD-2001}}]
        facts: ["Pronto, cancelei o pedido ORD-2001"]
"""


async def test_each_conversation_starts_from_the_seeded_state(
    client: AsyncClient, llm: ScriptedLLM, engine: AsyncEngine
) -> None:
    llm.on("[reset] cancela o 2001", cancel_order("2001"))
    llm.on_confirmation("[reset] sim", Confirmation.CONFIRM)
    sessions = session_factory(engine)

    report = await evaluate(
        client,
        parse_dataset(CANCELS_TWICE),
        FlagsReplies("never"),
        models=MODELS,
        started_at=STARTED_AT,
        reset=lambda: reset(sessions),
    )

    # The second Conversation cancels again: the first one's Cancellation was undone.
    assert [result.completed for result in report.conversations] == [True, True]


ATTENDANT_DECIDES = """
name: approvals
conversations:
  - id: rejected
    customer_id: cus_ana
    outcome: resolved
    turns:
      - message: "[approval] reembolso do 1001"
        tool_calls: [{name: create_refund, args: {order_id: ORD-1001}}]
        facts: ["aguardando aprovação"]
      - message: "[approval] e o reembolso?"
        attendant: {decision: reject, reason: "Produto sem defeito"}
        tool_calls: [{name: get_refund_status, args: {order_id: ORD-1001}}]
        facts: ["Motivo: Produto sem defeito", "recusado"]
"""


async def test_an_attendant_decides_the_pending_approval_before_a_turn(
    client: AsyncClient, llm: ScriptedLLM, engine: AsyncEngine
) -> None:
    llm.on("[approval] reembolso do 1001", refund("1001"))
    llm.on("[approval] e o reembolso?", refund("1001", status=True))
    sessions = session_factory(engine)

    report = await evaluate(
        client,
        parse_dataset(ATTENDANT_DECIDES),
        FlagsReplies("never"),
        models=MODELS,
        started_at=STARTED_AT,
        reset=lambda: reset(sessions),
    )

    [result] = report.conversations
    # The decision reaches the Customer as a notice before the turn's reply.
    assert result.turns[1].notices
    assert result.completed


def test_order_status_dataset_covers_every_logistics_scenario() -> None:
    dataset: Dataset = load_dataset(ORDER_STATUS_DATASET)

    asked = {
        call.args.get("order_id")
        for conversation in dataset.conversations
        for turn in conversation.turns
        for call in turn.tool_calls
        if call.name == "get_order_status"
    }
    scenarios = {
        "in transit": {"ORD-1002"},
        "delayed": {"ORD-2002"},
        "delivered": {"ORD-1001", "ORD-3002"},
        "lost": {"ORD-1004"},
        "returned": {"ORD-2004"},
        "timeout": {"ORD-3003"},
        "carrier 500": {"ORD-3004"},
        "carrier 404": {"ORD-3005"},
        "not dispatched": {"ORD-1003", "ORD-2001", "ORD-3001", "ORD-4001"},
    }
    assert {name for name, orders in scenarios.items() if not orders & asked} == set()
    assert len({conversation.id for conversation in dataset.conversations}) == len(
        dataset.conversations
    )


def test_every_dataset_scripts_seeded_customers_with_unique_ids() -> None:
    datasets = [load_dataset(path) for path in sorted(DATASETS.glob("*.yaml"))]
    conversations = [c for dataset in datasets for c in dataset.conversations]

    ids = [conversation.id for conversation in conversations]
    assert len(set(ids)) == len(ids)  # unique across datasets: reports are read side by side
    seeded = {customer["id"] for customer in CUSTOMERS}
    assert {conversation.customer_id for conversation in conversations} <= seeded
    tools = {call.name for c in conversations for turn in c.turns for call in turn.tool_calls}
    assert tools == {
        "get_order_status",
        "preview_cancellation",
        "cancel_order",
        "create_refund",
        "get_refund_status",
        "search_knowledge_base",
        "create_support_ticket",
    }


class FailsOn(FlagsReplies):
    """Fake judge that times out on replies mentioning `marker`."""

    async def judge(self, turn: JudgedTurn) -> Verdict:
        if self.marker in turn.reply:
            raise TimeoutError("judge timed out")
        return Verdict(hallucinated=False, unsupported_claims=[])


ONE_FAILS = """
name: failures
conversations:
  - id: unknown_customer
    customer_id: cus_nobody
    outcome: resolved
    turns: [{message: "[eval] cadê o 1002?"}]
  - id: judge_times_out
    customer_id: cus_ana
    outcome: resolved
    turns:
      - message: "[eval] cadê o 1002?"
        tool_calls: [{name: get_order_status, args: {order_id: ORD-1002}}]
  - id: still_runs
    customer_id: cus_carla
    outcome: resolved
    turns:
      - message: "[eval] e o 3003?"
        tool_calls: [{name: get_order_status, args: {order_id: ORD-3003}}]
"""


async def test_a_conversation_that_errors_fails_alone(
    client: AsyncClient, llm: ScriptedLLM
) -> None:
    script(llm)

    report = await evaluate(
        client, parse_dataset(ONE_FAILS), FailsOn("ORD-1002"), models=MODELS, started_at=STARTED_AT
    )

    results = {result.id: result for result in report.conversations}
    assert results["unknown_customer"].observed_outcome == "failed"
    assert results["unknown_customer"].error
    assert not results["unknown_customer"].completed
    # The reply was scored; only its hallucination check is missing.
    [turn] = results["judge_times_out"].turns
    assert turn.verdict is None
    assert turn.judge_error
    assert results["judge_times_out"].completed
    assert results["still_runs"].completed
    assert report.summary.conversations_failed == 1
    assert report.summary.judged_turns == 1
    markdown = render_markdown(report)
    assert "Failed to run" in markdown
    assert "Judge failed" in markdown


ONLY_LLM_WORDING = """
name: templates
conversations:
  - id: refund_template
    customer_id: cus_ana
    outcome: resolved
    turns:
      - message: "[eval] reembolso do 1001"
        tool_calls: [{name: create_refund, args: {order_id: ORD-1001, amount_cents: null}}]
  - id: cancellation_template
    customer_id: cus_ana
    outcome: resolved
    turns:
      - message: "[eval] cancela o 1002"
        tool_calls: [{name: preview_cancellation, args: {order_id: ORD-1002}}]
  - id: ungrounded_fallback
    customer_id: cus_ana
    outcome: resolved
    turns:
      - message: "[eval] quando chega o 1002?"
        tool_calls: [{name: get_order_status, args: {order_id: ORD-1002}}]
  - id: llm_wording
    customer_id: cus_ana
    outcome: resolved
    turns:
      - message: "[eval] cadê o 1002?"
        tool_calls: [{name: get_order_status, args: {order_id: ORD-1002}}]
"""


LLM_WORDING = "O ORD-1002 (rastreio BR100000002) está a caminho, previsão 03/09/2026."


async def test_only_the_wording_the_llm_wrote_is_judged(
    client: AsyncClient, llm: ScriptedLLM
) -> None:
    llm.on("[eval] reembolso do 1001", refund("1001"))
    llm.on("[eval] cancela o 1002", cancel_order("1002"))
    # A date the facts don't have: the reply falls back to the facts template.
    llm.on("[eval] quando chega o 1002?", order_status("1002"), reply="Chega em 25/12/2030.")
    llm.on("[eval] cadê o 1002?", order_status("1002"), reply=LLM_WORDING)
    judge = FlagsReplies("ORD-1002")

    report = await evaluate(
        client, parse_dataset(ONLY_LLM_WORDING), judge, models=MODELS, started_at=STARTED_AT
    )

    # Refund and Cancellation replies are templates rendered from the tool outputs.
    assert [turn.reply for turn in judge.judged] == [LLM_WORDING]
    results = {result.id: result for result in report.conversations}
    for template in ("refund_template", "cancellation_template", "ungrounded_fallback"):
        [turn] = results[template].turns
        assert turn.verdict is None
        assert turn.tool_calls
    assert report.summary.judged_turns == 1
