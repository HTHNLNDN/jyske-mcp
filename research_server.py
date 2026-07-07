# Research knowledge graph MCP server. Backed by lib/research_graph.py, which
# stores a relationally-modeled knowledge graph in its own SQLite file — this
# is institutional memory for research findings and past architecture
# decisions, separate from the banking data and its Alembic-tracked SQLite
# store in server.py.

from mcp.server.fastmcp import FastMCP

from lib.research_graph import ResearchGraph
from lib.research_schema import ResearchFacts

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
