"""The README's Mermaid diagrams are generated from the compiled graphs and kept fresh."""

from pathlib import Path

from support_agent.diagram import render_readme

README = Path(__file__).resolve().parents[1] / "README.md"


def test_readme_diagrams_match_the_compiled_graphs() -> None:
    readme = README.read_text()

    assert render_readme(readme) == readme, (
        "README graph diagram is out of date: run `uv run python -m support_agent.diagram`"
    )


def test_a_stale_diagram_is_regenerated_from_the_graph() -> None:
    stale = "\n".join(
        [
            "# Title",
            "<!-- graph:conversation -->",
            "```mermaid",
            "graph TD; old --> diagram;",
            "```",
            "<!-- /graph:conversation -->",
            "Between.",
            "<!-- graph:approval -->",
            "<!-- /graph:approval -->",
            "Footer.",
        ]
    )

    fresh = render_readme(stale)

    assert "old --> diagram" not in fresh
    assert "classify_intent -.-> consult_knowledge_base;" in fresh
    assert "await_decision --> settle;" in fresh
    assert fresh.startswith("# Title\n")
    assert "\nBetween.\n" in fresh
    assert fresh.endswith("Footer.")
    assert render_readme(fresh) == fresh
