"""Mermaid diagrams of the compiled graphs, kept in the README between markers.

`uv run python -m support_agent.diagram` rewrites them; a test fails when they are stale.
"""

import re
import sys
import uuid
from pathlib import Path
from typing import Any, cast

from langgraph.checkpoint.memory import InMemorySaver

from support_agent.approvals import build_approval_graph
from support_agent.graph import AgentDeps, build_graph

# Nodes only reach their dependencies when run; drawing the graph never does.
_UNUSED = cast(Any, None)


async def _never_settle(approval_request_id: uuid.UUID) -> None:
    raise AssertionError("the diagram never runs the workflow")


def diagrams() -> dict[str, str]:
    """Mermaid source per README marker name."""
    conversation = build_graph(InMemorySaver(), AgentDeps(llm=_UNUSED, tools=_UNUSED))
    approval = build_approval_graph(InMemorySaver(), _never_settle)
    return {
        "conversation": conversation.get_graph().draw_mermaid(),
        "approval": approval.get_graph().draw_mermaid(),
    }


def render_readme(readme: str) -> str:
    """The README with every marked diagram regenerated."""
    for name, mermaid in diagrams().items():
        block = re.compile(rf"(<!-- graph:{name} -->\n).*?(<!-- /graph:{name} -->)", re.DOTALL)
        match = block.search(readme)
        if match is None:
            raise ValueError(f"README has no <!-- graph:{name} --> block")
        fenced = f"```mermaid\n{mermaid.strip()}\n```\n"
        readme = readme[: match.end(1)] + fenced + readme[match.start(2) :]
    return readme


def main() -> None:
    readme = Path(sys.argv[1] if len(sys.argv) > 1 else "README.md")
    readme.write_text(render_readme(readme.read_text()))


if __name__ == "__main__":
    main()
