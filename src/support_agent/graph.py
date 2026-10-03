"""The agent graph. For now a single echo node; later classify_intent → … → respond."""

from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph


class ConversationState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    intent: str


@dataclass(frozen=True)
class Turn:
    reply: str
    intent: str
    pending_actions: list[dict[str, Any]] = field(default_factory=list)


async def echo(state: ConversationState) -> ConversationState:
    last = state["messages"][-1]
    return {"messages": [AIMessage(content=f"Você disse: {last.content}")], "intent": "other"}


def build_graph(checkpointer: BaseCheckpointSaver[Any]) -> CompiledStateGraph[Any]:
    builder = StateGraph(ConversationState)
    builder.add_node("echo", echo)
    builder.add_edge(START, "echo")
    builder.add_edge("echo", END)
    return builder.compile(checkpointer=checkpointer)


async def run_turn(graph: CompiledStateGraph[Any], thread_id: str, message: str) -> Turn:
    """Run one Customer message through the graph on the Conversation's thread."""
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content=message)]},
        config={"configurable": {"thread_id": thread_id}},
    )
    return Turn(reply=str(result["messages"][-1].content), intent=result["intent"])
