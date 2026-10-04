"""The Markdown rendering of an evaluation report, for people to read and review."""

import json
from collections.abc import Sequence

from support_agent.evaluation.dataset import ExpectedToolCall
from support_agent.evaluation.report import (
    ConversationResult,
    LatencyStats,
    Report,
    tool_outputs,
)
from support_agent.evaluation.runner import ToolCall


def render_markdown(report: Report) -> str:
    summary, models = report.summary, report.models
    lines = [
        f"# Evaluation: {report.dataset} — {report.started_at:%Y-%m-%d}",
        "",
        f"- Started at: {report.started_at.isoformat()}",
        f"- Models ({models.provider}): classifier `{models.classifier}`, "
        f"responder `{models.responder}`, judge `{models.judge}`",
        f"- Commit: `{report.git_commit or 'unknown'}`",
        f"- Conversations: {summary.conversations} ({summary.turns} Customer turns)",
        "",
        "## Metrics",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Task completion | {_pct(summary.task_completion_rate)} |",
        f"| Conversations failed to run | {summary.conversations_failed} |",
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
        if result.error:
            issues.append(f"- `{result.id}`: Failed to run: {result.error}")
            continue
        if result.observed_outcome != result.expected_outcome.observed_as:
            issues.append(
                f"- `{result.id}`: expected {result.expected_outcome.value}, "
                f"observed {result.observed_outcome}"
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
            if turn.judge_error:
                issues.append(f"{where}: Judge failed: {turn.judge_error}")
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
        lines += [f"**Customer:** {turn.message}", "", "**Agent:**", ""]
        for text in [*turn.notices, turn.reply]:
            lines += [f"> {line}" if line else ">" for line in text.splitlines()]
        lines.append("")
        if turn.judge_error:
            lines += [f"_Judge failed: {turn.judge_error}_", ""]
            continue
        if turn.verdict is None:
            reason = "Template reply" if turn.tool_calls else "No tool output"
            lines += [f"_{reason}: not judged._", ""]
            continue
        verdict = (
            f"hallucinated — {turn.verdict.unsupported_claims}"
            if turn.verdict.hallucinated
            else "grounded"
        )
        lines += [
            "<details><summary>Tool outputs</summary>",
            "",
            "```json",
            json.dumps(tool_outputs(turn.tool_calls), ensure_ascii=False, indent=2),
            "```",
            "",
            "</details>",
            "",
            f"- [ ] Judge: {verdict}",
            "",
        ]
    return lines
