# Research knowledge graph MCP server — dev/agent tooling, not product code.
# Backed by graph.py (same directory), which stores a relationally-modeled
# knowledge graph in its own SQLite file — this is institutional memory for
# research findings and past architecture decisions, separate from the banking
# data and its Alembic-tracked SQLite store in jyske_mcp.mcp.server.
#
# Invoked by absolute path as a stdio MCP server (see ~/.claude.json), so the
# sibling modules are put on sys.path here instead of installing a package.

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.fastmcp import FastMCP

from graph import ResearchGraph
from schema import ResearchFacts

mcp = FastMCP("research")
graph = ResearchGraph()


# ── tools ────────────────────────────────────────────────────────────────────

@mcp.tool()
def get_research_context(topic: str) -> str:
    """Return a digest of stored institutional knowledge (library evaluations,
    API quirks, concepts, past decisions) matching `topic`. Call this before
    deciding whether external research is needed."""
    return graph.get_context(topic)


@mcp.tool()
def record_decision(decision: str, rationale: str, alternatives_rejected: list[str] | str) -> str:
    """Persist an architectural decision to institutional memory."""
    graph.record_decision(decision, rationale, alternatives_rejected)
    return f"Decision recorded: {decision}"


@mcp.tool()
def record_research_facts(facts_json: str) -> str:
    """Persist structured research findings to the knowledge graph. facts_json
    is a JSON object matching the ResearchFacts schema (topic, libraries[],
    apis[], concepts[], sources[]). Call after writing a research brief."""
    facts = ResearchFacts.model_validate_json(facts_json)
    counts = graph.record_facts(facts)
    return f"Stored: {counts}"


if __name__ == "__main__":
    mcp.run()
