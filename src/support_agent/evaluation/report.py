"""Runs a dataset, scores each Conversation against its labels and renders the report.

Task completion: the observed outcome is the expected one and every turn's reply has its
required facts. The observed outcome is `escalated` if a Ticket was opened, `clarification`
if the last turn answered without calling a tool (nor settling a pending Cancellation), and
`resolved` otherwise. An escalation is appropriate when the Conversation expected one and
avoidable otherwise. Tool calls are compared per turn with retries collapsed. Hallucinations
are judged on the turns whose reply rests on tool outputs.
"""

import json
import math
import random
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from httpx import AsyncClient
from pydantic import BaseModel, ValidationError

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
    ToolCall,
    TurnRun,
    run_conversation,
)
from support_agent.tools import OrderIdArgs

REVIEW_FRACTION = 0.1
TICKET_TOOL = "create_support_ticket"
# Nodes that act on the Customer's answer to a Cancellation question without a new tool call.
CONFIRMATION_NODES = frozenset({"await_confirmation", "cancel"})

ObservedOutcome = Literal["resolved", "escalated", "clarification"]


class ModelIds(BaseModel):
    classifier: str
    responder: str
    judge: str


class TurnResult(BaseModel):
    customer: str
    reply: str
    expected_intent: str | None
    intent: str
    expected_tool_calls: list[ExpectedToolCall]
    tool_calls: list[ToolCall]  # retries collapsed
    tool_calls_ok: bool
    missing_facts: list[str]
    verdict: Verdict | None  # None: no tool output to judge against
    latency_ms: float
    nodes: list[NodeTiming]

    @property
    def intent_ok(self) -> bool | None:
        return None if self.expected_intent is None else self.intent == self.expected_intent


class ConversationResult(BaseModel):
    id: str
    customer: str
    description: str | None
    expected_outcome: Outcome
    observed_outcome: ObservedOutcome
    escalation: Literal["appropriate", "avoidable"] | None
    completed: bool
    turns: list[TurnResult]


class LatencyStats(BaseModel):
    count: int
    p50: float
    p95: float


class Latency(BaseModel):
    turn: LatencyStats  # end to end, per Customer turn
    nodes: dict[str, LatencyStats]  # per node execution


class Summary(BaseModel):
    conversations: int
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
) -> Report:
    """Run every Conversation through the app (one after another), then judge and score."""
    results = []
    for conversation in dataset.conversations:
        run = await run_conversation(client, conversation)
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


async def _score(
    conversation: ScriptedConversation, run: ConversationRun, judge: Judge
) -> ConversationResult:
    turns = [
        await _score_turn(expected, observed, judge)
        for expected, observed in zip(conversation.turns, run.turns, strict=True)
    ]
    observed_outcome = _observed_outcome(run.turns)
    expects_ticket = conversation.outcome is Outcome.ESCALATED_APPROPRIATE
    outcome_ok = observed_outcome == ("escalated" if expects_ticket else conversation.outcome.value)
    escalation: Literal["appropriate", "avoidable"] | None = None
    if observed_outcome == "escalated":
        escalation = "appropriate" if expects_ticket else "avoidable"
    return ConversationResult(
        id=conversation.id,
        customer=conversation.customer,
        description=conversation.description,
        expected_outcome=conversation.outcome,
        observed_outcome=observed_outcome,
        escalation=escalation,
        completed=outcome_ok and not any(turn.missing_facts for turn in turns),
        turns=turns,
    )


async def _score_turn(expected: CustomerTurn, observed: TurnRun, judge: Judge) -> TurnResult:
    calls = _collapse_retries(observed.tool_calls)
    verdict = None
    if calls:
        verdict = await judge.judge(
            JudgedTurn(
                customer=observed.customer,
                reply=observed.reply,
                tool_outputs=[call.model_dump(mode="json") for call in calls],
            )
        )
    reply = observed.reply.casefold()
    return TurnResult(
        customer=observed.customer,
        reply=observed.reply,
        expected_intent=expected.intent,
        intent=observed.intent,
        expected_tool_calls=expected.tool_calls,
        tool_calls=calls,
        tool_calls_ok=_calls_match(expected.tool_calls, calls),
        missing_facts=[
            " | ".join(options)
            for options in ([fact] if isinstance(fact, str) else fact for fact in expected.facts)
            if not any(option.casefold() in reply for option in options)
        ],
        verdict=verdict,
        latency_ms=observed.latency_ms,
        nodes=observed.nodes,
    )


def _observed_outcome(turns: Sequence[TurnRun]) -> ObservedOutcome:
    if any(call.name == TICKET_TOOL for turn in turns for call in turn.tool_calls):
        return "escalated"
    last = turns[-1]
    if not last.tool_calls and not CONFIRMATION_NODES & {node.node for node in last.nodes}:
        return "clarification"
    return "resolved"


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
        turns=len(turns),
        task_completion_rate=_rate([result.completed for result in results]) or 0.0,
        tool_call_accuracy=_rate([turn.tool_calls_ok for turn in turns]) or 0.0,
        intent_accuracy=_rate(intents),
        hallucination_rate=_rate([verdict.hallucinated for verdict in verdicts]),
        judged_turns=len(verdicts),
        escalation_rate_appropriate=_rate([r.escalation == "appropriate" for r in results]) or 0.0,
        escalation_rate_avoidable=_rate([r.escalation == "avoidable" for r in results]) or 0.0,
        escalations_missed=sum(
            r.expected_outcome is Outcome.ESCALATED_APPROPRIATE and r.escalation is None
            for r in results
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


def write_report(report: Report, directory: Path) -> tuple[Path, Path]:
    """Write `<started_at>-<dataset>.json` and `.md` into directory; returns both paths."""
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{report.started_at:%Y-%m-%dT%H%MZ}-{report.dataset}"
    json_path, markdown_path = directory / f"{stem}.json", directory / f"{stem}.md"
    json_path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def render_markdown(report: Report) -> str:
    summary, models = report.summary, report.models
    lines = [
        f"# Evaluation: {report.dataset} — {report.started_at:%Y-%m-%d}",
        "",
        f"- Started at: {report.started_at.isoformat()}",
        f"- Models: classifier `{models.classifier}`, responder `{models.responder}`, "
        f"judge `{models.judge}`",
        f"- Commit: `{report.git_commit or 'unknown'}`",
        f"- Conversations: {summary.conversations} ({summary.turns} Customer turns)",
        "",
        "## Metrics",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Task completion | {_pct(summary.task_completion_rate)} |",
        f"| Tool-call accuracy (per turn) | {_pct(summary.tool_call_accuracy)} |",
        f"| Intent accuracy (per turn) | {_pct(summary.intent_accuracy)} |",
        f"| Hallucination rate ({summary.judged_turns} judged turns) "
        f"| {_pct(summary.hallucination_rate)} |",
        f"| Escalation rate, appropriate | {_pct(summary.escalation_rate_appropriate)} |",
        f"| Escalation rate, avoidable | {_pct(summary.escalation_rate_avoidable)} |",
        f"| Escalations missed | {summary.escalations_missed} |",
        "",
        "## Latency (ms)",
        "",
        "| Stage | n | p50 | p95 |",
        "|---|---|---|---|",
        _latency_row("turn (end to end)", summary.latency_ms.turn),
        *(_latency_row(f"`{node}`", stats) for node, stats in summary.latency_ms.nodes.items()),
        "",
        "## Conversations",
        "",
        "| Conversation | Expected | Observed | Completed | Tool calls | Facts | Judge |",
        "|---|---|---|---|---|---|---|",
        *(_conversation_row(result) for result in report.conversations),
        "",
        "## Issues",
        "",
        *(_issues(report.conversations) or ["None."]),
        "",
        "## Human review sample",
        "",
        "Check the judge: tick each verdict you agree with, note the ones you don't.",
        "",
    ]
    for result in report.conversations:
        if result.id in report.review_sample:
            lines.extend(_transcript(result))
    return "\n".join(lines).rstrip() + "\n"


def _pct(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate:.0%}"


def _latency_row(stage: str, stats: LatencyStats) -> str:
    return f"| {stage} | {stats.count} | {stats.p50:.0f} | {stats.p95:.0f} |"


def _conversation_row(result: ConversationResult) -> str:
    observed: str = result.observed_outcome
    if result.escalation is not None:
        observed = f"{observed} ({result.escalation})"
    tools_ok = sum(turn.tool_calls_ok for turn in result.turns)
    facts_ok = sum(not turn.missing_facts for turn in result.turns)
    flagged = sum(bool(turn.verdict and turn.verdict.hallucinated) for turn in result.turns)
    judged = sum(turn.verdict is not None for turn in result.turns)
    return (
        f"| `{result.id}` | {result.expected_outcome.value} | {observed} "
        f"| {'yes' if result.completed else '**no**'} | {tools_ok}/{len(result.turns)} "
        f"| {facts_ok}/{len(result.turns)} | {flagged}/{judged} flagged |"
    )


def _issues(results: Sequence[ConversationResult]) -> list[str]:
    issues = []
    for result in results:
        expected = result.expected_outcome.value
        if result.observed_outcome != expected and not (
            result.escalation == "appropriate" and expected == Outcome.ESCALATED_APPROPRIATE
        ):
            issues.append(
                f"- `{result.id}`: expected {expected}, observed {result.observed_outcome}"
            )
        for number, turn in enumerate(result.turns, start=1):
            where = f"- `{result.id}` turn {number}"
            if turn.intent_ok is False:
                issues.append(f"{where}: Intent {turn.intent}, expected {turn.expected_intent}")
            if not turn.tool_calls_ok:
                issues.append(
                    f"{where}: tool calls {_calls(turn.tool_calls)}, "
                    f"expected {_calls(turn.expected_tool_calls)}"
                )
            if turn.missing_facts:
                issues.append(f"{where}: missing facts {turn.missing_facts}")
            if turn.verdict and turn.verdict.hallucinated:
                issues.append(f"{where}: unsupported claims {turn.verdict.unsupported_claims}")
    return issues


def _calls(calls: Sequence[ToolCall | ExpectedToolCall]) -> str:
    if not calls:
        return "none"
    return ", ".join(
        f"{call.name}({json.dumps(call.args, ensure_ascii=False, default=str)})" for call in calls
    )


def _transcript(result: ConversationResult) -> list[str]:
    lines = [f"### `{result.id}`", ""]
    if result.description:
        lines += [result.description, ""]
    for turn in result.turns:
        lines += [f"**Customer:** {turn.customer}", "", "**Agent:**", ""]
        lines += [f"> {line}" if line else ">" for line in turn.reply.splitlines()]
        lines.append("")
        if turn.verdict is None:
            lines += ["_No tool output: not judged._", ""]
            continue
        outputs: list[dict[str, Any]] = [call.model_dump(mode="json") for call in turn.tool_calls]
        verdict = (
            f"hallucinated — {turn.verdict.unsupported_claims}"
            if turn.verdict.hallucinated
            else "grounded"
        )
        lines += [
            "<details><summary>Tool outputs</summary>",
            "",
            "```json",
            json.dumps(outputs, ensure_ascii=False, indent=2),
            "```",
            "",
            "</details>",
            "",
            f"- [ ] Judge: {verdict}",
            "",
        ]
    return lines
