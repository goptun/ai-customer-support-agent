from collections.abc import Sequence

from support_agent.llm import Classification, ConversationMessage, Entities, Intent


def order_status(order_id: str | None = None) -> Classification:
    return Classification(
        intents=[Intent.ORDER_STATUS], confidence=0.95, entities=Entities(order_id=order_id)
    )


class ScriptedLLM:
    """Fake LLM scripted by the Customer's message.

    Unscripted messages classify as `other`; unscripted replies repeat the facts verbatim,
    like a perfectly grounded model would.
    """

    def __init__(self) -> None:
        self._classifications: dict[str, Classification] = {}
        self._replies: dict[str, str] = {}

    def on(self, message: str, classification: Classification, reply: str | None = None) -> None:
        self._classifications[message] = classification
        if reply is not None:
            self._replies[message] = reply

    async def classify(self, history: Sequence[ConversationMessage]) -> Classification:
        return self._classifications.get(
            _last_customer_message(history),
            Classification(intents=[Intent.OTHER], confidence=1.0, entities=Entities()),
        )

    async def respond(self, history: Sequence[ConversationMessage], facts: str) -> str:
        return self._replies.get(_last_customer_message(history), facts)


def _last_customer_message(history: Sequence[ConversationMessage]) -> str:
    return next(m.content for m in reversed(history) if m.role == "user")
