"""LLM judge for hallucinations: reply claims the turn's tool outputs don't support."""

import json
from typing import Any, Protocol

from anthropic import AsyncAnthropic
from pydantic import BaseModel, Field


class JudgedTurn(BaseModel):
    message: str  # the Customer's
    reply: str
    tool_outputs: list[dict[str, Any]]  # {name, args, output} per distinct call


class Verdict(BaseModel):
    unsupported_claims: list[str] = Field(
        description="Each claim in the reply the tool outputs don't support, quoted or close"
    )
    hallucinated: bool = Field(description="True if any unsupported claim was found")


class Judge(Protocol):
    async def judge(self, turn: JudgedTurn) -> Verdict: ...


JUDGE_SYSTEM = """\
You check replies of the customer support agent of a Brazilian online store for \
hallucinations. The agent may state only facts found in the outputs of the tools it called \
while answering this message.

List every factual claim in the reply — about the Customer's orders, shipments, refunds, \
tickets, dates, amounts, statuses, carrier events, tracking codes or store policies — that the \
tool outputs do not support. Dates in the reply are DD/MM/AAAA while tool outputs use ISO \
dates: the same date in another format is supported, and so are statuses and events \
translated to Portuguese and day counts that follow from the dates. Greetings, offers of \
help, questions to the Customer, what the agent can do and how support works (e.g. that an \
attendant will follow up) are not claims. Set hallucinated to true if you list any claim."""


class AnthropicJudge:
    def __init__(self, client: AsyncAnthropic, model: str) -> None:
        self._client = client
        self._model = model

    async def judge(self, turn: JudgedTurn) -> Verdict:
        response = await self._client.messages.parse(
            model=self._model,
            max_tokens=1024,
            system=JUDGE_SYSTEM,
            messages=[{"role": "user", "content": _prompt(turn)}],
            output_format=Verdict,
        )
        if response.parsed_output is None:
            raise RuntimeError(f"judge gave no verdict (stop reason {response.stop_reason})")
        return response.parsed_output


def _prompt(turn: JudgedTurn) -> str:
    outputs = json.dumps(turn.tool_outputs, ensure_ascii=False, indent=2, default=str)
    return (
        f"<customer_message>\n{turn.message}\n</customer_message>\n\n"
        f"<tool_outputs>\n{outputs}\n</tool_outputs>\n\n"
        f"<agent_reply>\n{turn.reply}\n</agent_reply>"
    )
