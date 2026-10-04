"""Runs a dataset and scores each Conversation against its labels into a report.

Task completion: the observed outcome is the expected one and every turn's reply has its
required facts. The observed outcome is `escalated` if a Ticket was opened, `clarification`
if the last turn answered without calling a tool (nor settling a pending Cancellation), and
`resolved` otherwise. An escalation is appropriate when the Conversation expected one and
avoidable otherwise. Tool calls are compared per turn with retries collapsed. Hallucinations
are judged on the wording the responder LLM wrote from tool outputs: Refund, Cancellation,
error and Ticket replies are templates rendered from those outputs, so they aren't judged.
"""

import math
import random
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any, Literal

from httpx import AsyncClient
from pydantic import BaseModel, Field, ValidationError

from support_agent.evaluation.dataset import (
    CustomerTurn,
    Dataset,
    ExpectedToolCall,
    Outcome,
    ScriptedConversation,
)
from support_agent.evaluation.judge import Judge, JudgedTurn, Verdict
from support_agent.evaluation.runner import (
    ConversationRun,
    NodeTiming,
    ResponderFallback,
    ToolCall,
    TurnRun,
    run_conversation,
)
from support_agent.llm import Intent
from support_agent.tools import OrderIdArgs, create_support_ticket

REVIEW_FRACTION = 0.1
TICKET_TOOL = create_support_ticket.__name__
# Nodes that act on the Customer's answer to a Cancellation question without a new tool call.
CONFIRMATION_NODES = frozenset({"await_confirmation", "cancel"})

# failed: the Conversation could not be run to the end (e.g. the LLM endpoint timed out).
ObservedOutcome = Literal["resolved", "escalated", "clarification", "failed"]


class ModelIds(BaseModel):
    provider: str  # LLM_PROVIDER: whose models these are
    classifier: str
    responder: str
    judge: str


class TurnResult(BaseModel):
    message: str
    reply: str
    notices: list[str] = []
    expected_intent: Intent | None
    intent: str
    expected_tool_calls: list[ExpectedToolCall]
    tool_calls: list[ToolCall]  # retries collapsed
    tool_calls_ok: bool
    missing_facts: list[str]
    verdict: Verdict | None  # None: not judged (no tool output, template reply) or judge failed
    judge_error: str | None = None
    latency_ms: float
    nodes: list[NodeTiming]
    # Only on turns whose LLM reply fell back to the facts: what it wrote, and why.
    fallback: ResponderFallback | None = Field(default=None, exclude_if=lambda value: value is None)

    @property
    def intent_ok(self) -> bool | None:
        return None if self.expected_intent is None else self.intent == self.expected_intent


class ConversationResult(BaseModel):
    id: str
    customer_id: str
    description: str | None
    expected_outcome: Outcome
    observed_outcome: ObservedOutcome
    escalation: Literal["appropriate", "avoidable"] | None
    completed: bool
    turns: list[TurnResult]
    error: str | None = None  # why it failed


class LatencyStats(BaseModel):
    count: int
    p50: float
    p95: float


class Latency(BaseModel):
    turn: LatencyStats  # end to end, per Customer turn
    nodes: dict[str, LatencyStats]  # per node execution


class Summary(BaseModel):
    conversations: int
    conversations_failed: int  # could not be run to the end; count as not completed
    turns: int
    task_completion_rate: float
    tool_call_accuracy: float  # turns whose tool calls match the gold ones
    intent_accuracy: float | None  # over turns labelled with an Intent
    hallucination_rate: float | None  # over judged turns
    judged_turns: int
    escalation_rate_appropriate: float
    escalation_rate_avoidable: float
    escalations_missed: int  # expected a Ticket, none opened
    latency_ms: Latency


class Report(BaseModel):
    dataset: str
    started_at: datetime
    models: ModelIds
    git_commit: str | None
    summary: Summary
    conversations: list[ConversationResult]
    review_sample: list[str]  # Conversation ids for a human to check the judge on


async def evaluate(
    client: AsyncClient,
    dataset: Dataset,
    judge: Judge,
    *,
    models: ModelIds,
    started_at: datetime,
    git_commit: str | None = None,
    seed: int = 0,
    reset: Callable[[], Awaitable[None]] | None = None,
) -> Report:
    """Run every Conversation through the app (one after another), then judge and score.

    `reset`, called before each Conversation, puts the data back as seeded: a Cancellation or
    Refund in one Conversation must not change what the next one finds.
    """
    results = []
    for conversation in dataset.conversations:
        if reset is not None:
            await reset()
        try:
            run = await run_conversation(client, conversation)
        except Exception as error:  # one broken Conversation must not lose the whole run
            results.append(_failed(conversation, error))
            continue
        results.append(await _score(conversation, run, judge))
    ids = [result.id for result in results]
    sampled = set(random.Random(seed).sample(ids, max(1, round(len(ids) * REVIEW_FRACTION))))
    return Report(
        dataset=dataset.name,
        started_at=started_at,
        models=models,
        git_commit=git_commit,
        summary=_summarize(results),
        conversations=results,
        review_sample=[id_ for id_ in ids if id_ in sampled],
    )


def _failed(conversation: ScriptedConversation, error: Exception) -> ConversationResult:
    return ConversationResult(
        id=conversation.id,
        customer_id=conversation.customer_id,
        description=conversation.description,
        expected_outcome=conversation.outcome,
        observed_outcome="failed",
        escalation=None,
        completed=False,
        turns=[],
        error=f"{type(error).__name__}: {error}",
    )


async def _score(
    conversation: ScriptedConversation, run: ConversationRun, judge: Judge
) -> ConversationResult:
    turns = [
        await _score_turn(expected, observed, judge)
        for expected, observed in zip(conversation.turns, run.turns, strict=True)
    ]
    observed_outcome = _observed_outcome(run.turns)
    escalation: Literal["appropriate", "avoidable"] | None = None
    if observed_outcome == "escalated":
        escalation = "appropriate" if conversation.outcome.expects_ticket else "avoidable"
    return ConversationResult(
        id=conversation.id,
        customer_id=conversation.customer_id,
        description=conversation.description,
        expected_outcome=conversation.outcome,
        observed_outcome=observed_outcome,
        escalation=escalation,
        completed=observed_outcome == conversation.outcome.observed_as
        and not any(turn.missing_facts for turn in turns),
        turns=turns,
    )


async def _score_turn(expected: CustomerTurn, observed: TurnRun, judge: Judge) -> TurnResult:
    calls = _collapse_retries(observed.tool_calls)
    verdict, judge_error = None, None
    if calls and observed.llm_wording is not None:
        try:
            verdict = await judge.judge(
                JudgedTurn(
                    message=observed.message,
                    reply=observed.llm_wording,
                    tool_outputs=tool_outputs(calls),
                )
            )
        except Exception as error:  # the reply is still scored, just not judged
            judge_error = f"{type(error).__name__}: {error}"
    shown = "\n".join([*observed.notices, observed.reply]).casefold()
    return TurnResult(
        message=observed.message,
        reply=observed.reply,
        notices=observed.notices,
        expected_intent=expected.intent,
        intent=observed.intent,
        expected_tool_calls=expected.tool_calls,
        tool_calls=calls,
        tool_calls_ok=_calls_match(expected.tool_calls, calls),
        missing_facts=[
            " | ".join(options)
            for options in ([fact] if isinstance(fact, str) else fact for fact in expected.facts)
            if not any(option.casefold() in shown for option in options)
        ],
        verdict=verdict,
        judge_error=judge_error,
        latency_ms=observed.latency_ms,
        nodes=observed.nodes,
        fallback=observed.fallback,
    )


def _observed_outcome(turns: Sequence[TurnRun]) -> ObservedOutcome:
    if any(_opened_ticket(call) for turn in turns for call in turn.tool_calls):
        return "escalated"
    last = turns[-1]
    if not last.tool_calls and not CONFIRMATION_NODES & {node.node for node in last.nodes}:
        return "clarification"
    return "resolved"


def _opened_ticket(call: ToolCall) -> bool:
    # A failed call outputs a ToolError (a code, no ticket_id): no Ticket, no escalation.
    return call.name == TICKET_TOOL and "ticket_id" in (call.output or {})


def tool_outputs(calls: Sequence[ToolCall]) -> list[dict[str, Any]]:
    return [call.model_dump(mode="json") for call in calls]


def _collapse_retries(calls: Sequence[ToolCall]) -> list[ToolCall]:
    """Repeated identical calls are one call retried; its last output is the one used."""
    collapsed: list[ToolCall] = []
    for call in calls:
        if collapsed and (collapsed[-1].name, collapsed[-1].args) == (call.name, call.args):
            collapsed[-1] = call
        else:
            collapsed.append(call)
    return collapsed


def _calls_match(expected: Sequence[ExpectedToolCall], observed: Sequence[ToolCall]) -> bool:
    return len(expected) == len(observed) and all(
        gold.name == call.name
        and all(_same_arg(key, value, call.args.get(key)) for key, value in gold.args.items())
        for gold, call in zip(expected, observed, strict=True)
    )


def _same_arg(key: str, expected: object, observed: object) -> bool:
    if key == "order_id":  # "1002", "#1002" and "ORD-1002" name the same Order
        return _order_id(expected) == _order_id(observed)
    return expected == observed


def _order_id(value: object) -> object:
    try:
        return OrderIdArgs.model_validate({"order_id": value}).order_id
    except ValidationError:
        return value


def _summarize(results: Sequence[ConversationResult]) -> Summary:
    turns = [turn for result in results for turn in result.turns]
    intents = [turn.intent_ok for turn in turns if turn.intent_ok is not None]
    verdicts = [turn.verdict for turn in turns if turn.verdict is not None]
    node_ms: defaultdict[str, list[float]] = defaultdict(list)
    for turn in turns:
        for node in turn.nodes:
            node_ms[node.node].append(node.ms)
    return Summary(
        conversations=len(results),
        conversations_failed=sum(result.observed_outcome == "failed" for result in results),
        turns=len(turns),
        task_completion_rate=_rate([result.completed for result in results]) or 0.0,
        tool_call_accuracy=_rate([turn.tool_calls_ok for turn in turns]) or 0.0,
        intent_accuracy=_rate(intents),
        hallucination_rate=_rate([verdict.hallucinated for verdict in verdicts]),
        judged_turns=len(verdicts),
        escalation_rate_appropriate=_rate([r.escalation == "appropriate" for r in results]) or 0.0,
        escalation_rate_avoidable=_rate([r.escalation == "avoidable" for r in results]) or 0.0,
        escalations_missed=sum(
            r.expected_outcome.expects_ticket and r.escalation is None for r in results
        ),
        latency_ms=Latency(
            turn=_stats([turn.latency_ms for turn in turns]),
            nodes={node: _stats(ms) for node, ms in sorted(node_ms.items())},
        ),
    )


def _rate(flags: Sequence[bool]) -> float | None:
    return sum(flags) / len(flags) if flags else None


def _stats(values: Sequence[float]) -> LatencyStats:
    return LatencyStats(
        count=len(values), p50=percentile(values, 0.5), p95=percentile(values, 0.95)
    )


def percentile(values: Sequence[float], q: float) -> float:
    """Linear interpolation between the closest ranks (numpy's default)."""
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low = math.floor(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)
