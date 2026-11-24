"""In-cluster MCP servers (read-only by deployment, SEC-01). No gateway, no auth (D-04)."""
import os

from langchain_mcp_adapters.client import MultiServerMCPClient

# Measured on this cluster (2026-11-24): query_loki_patterns -> 404 (pattern ingester is off);
# query_loki_stats -> "entries: 0" for streams with hundreds of recent lines (index-only, misses
# unflushed chunks), a false "no logs"; analyze_loki_labels audits label design, not incidents.
# Dropping them also removes their schemas from every model call.
DROPPED_TOOLS = {"query_loki_patterns", "query_loki_stats", "analyze_loki_labels"}


async def load_tools() -> list:
    client = MultiServerMCPClient({
        "grafana": {"transport": "streamable_http", "url": os.environ["GRAFANA_MCP_URL"]},
        "kubernetes": {"transport": "streamable_http", "url": os.environ["K8S_MCP_URL"]},
    })
    return [t for t in await client.get_tools() if t.name not in DROPPED_TOOLS]
