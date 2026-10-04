"""Scripted Conversations in YAML, each labelled with what the agent should do.

A Conversation has fixed Customer turns and an expected outcome; each turn may name the
expected Intent, the tool calls the agent should make while answering it (name + the args
that matter) and the facts its reply must contain.
"""

import enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from support_agent.llm import Intent


class Outcome(enum.StrEnum):
    RESOLVED = "resolved"
    ESCALATED_APPROPRIATE = "escalated_appropriate"  # a Ticket is the right answer
    CLARIFICATION = "clarification"  # the agent should ask instead of acting

    @property
    def expects_ticket(self) -> bool:
        return self is Outcome.ESCALATED_APPROPRIATE

    @property
    def observed_as(self) -> str:
        """The observed outcome that meets it: an appropriate escalation is a Ticket opened."""
        return "escalated" if self.expects_ticket else self.value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExpectedToolCall(_Strict):
    name: str
    # Only these args are compared; others the agent passes (e.g. a summary) are free.
    args: dict[str, Any] = Field(default_factory=dict)


class CustomerTurn(_Strict):
    message: str = Field(min_length=1)  # what the Customer says
    intent: Intent | None = None
    tool_calls: list[ExpectedToolCall] = Field(default_factory=list)  # none expected by default
    # Each fact must appear in the reply (case-insensitive); a list means any one of them.
    facts: list[str | list[str]] = Field(default_factory=list)


class ScriptedConversation(_Strict):
    id: str
    customer_id: str  # a seeded Customer
    outcome: Outcome
    description: str | None = None
    turns: list[CustomerTurn] = Field(min_length=1)


class Dataset(_Strict):
    name: str
    conversations: list[ScriptedConversation] = Field(min_length=1)


def parse_dataset(text: str) -> Dataset:
    return Dataset.model_validate(yaml.safe_load(text))


def load_dataset(path: Path) -> Dataset:
    return parse_dataset(path.read_text(encoding="utf-8"))
