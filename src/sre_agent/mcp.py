"""In-cluster MCP servers (read-only by deployment, SEC-01). No gateway, no auth (D-04)."""
import os

from langchain_mcp_adapters.client import MultiServerMCPClient


async def load_tools() -> list:
    client = MultiServerMCPClient({
        "grafana": {"transport": "streamable_http", "url": os.environ["GRAFANA_MCP_URL"]},
        "kubernetes": {"transport": "streamable_http", "url": os.environ["K8S_MCP_URL"]},
    })
    return await client.get_tools()
