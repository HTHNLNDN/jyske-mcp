# Thin re-export shim: kept ONLY so `python -m jyske_mcp.mcp.server` (the
# Claude Desktop MCP entrypoint documented in README.md) keeps working
# unchanged. All 23 tool implementations + the FastMCP server instance now
# live in jyske_mcp/slices/finance/tools.py (relocated at epic deliverable
# #7a — .agent/epics/vsa-restructure-blueprint.md §4); this module has no
# logic of its own.
from jyske_mcp.slices.finance.tools import mcp

if __name__ == "__main__":
    mcp.run()
