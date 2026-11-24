"""sre_agent: diagnoses Alertmanager alerts for pulsar/voyager and, through the fix-agent
subagent, proposes a Helm values fix as a PR on sre-gitops (behind human approval).

Served by the LangGraph Agent Server. AGENTS.md and skills ship in the image (context/)
and are upserted into the Store at startup, so the agent reads them through StoreBackend.
"""
import asyncio
import os
import pathlib
from typing import Literal

from deepagents import FilesystemPermission, create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend, StoreBackend
from deepagents.backends.utils import create_file_data
from langchain.agents.structured_output import ToolStrategy
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph_sdk.runtime import ServerRuntime
from pydantic import BaseModel, Field

from sre_agent.mcp import load_tools
from sre_agent.tools import GITOPS_ROOT, describe_fix, propose_fix_pr, recent_changes, validate_fix_tool

CONTEXT_NS = ("sre_agent", "context")
MEMORY_NS = ("sre_agent", "memories")
CONTEXT_DIR = pathlib.Path(__file__).parent / "context"


class FixProposal(BaseModel):
    app: Literal["pulsar", "voyager", "grafana", "loki", "prometheus", "fluent-bit"]
    file_path: str
    change: str = Field(description='one line, e.g. "faultInjection.failEvery: 3 -> 0"')


class DiagnosisResult(BaseModel):
    summary: str = Field(description="1-2 sentences")
    root_cause: str
    confidence: Literal["low", "medium", "high"]
    evidence: list[str] = Field(description="each item cites its source: tool + query/resource")
    affected: list[str] = Field(description='e.g. "default/deployment/pulsar"')
    suspected_change: str | None = Field(description="commit sha + file, if found")
    fix: FixProposal | None
    pr_url: str | None = Field(description="ONLY the URL returned by propose_fix_pr, else null")


SYSTEM_PROMPT = (
    "You are sre_agent, the first responder for alerts on a Kubernetes cluster managed by "
    "ArgoCD from the sre-gitops repo. Follow the workflow in your AGENTS.md memory."
)

FIX_AGENT_PROMPT = (
    "You turn a finished diagnosis into ONE minimal Helm values change. Follow your helm-fix-pr "
    "skill: read the current file under /gitops/main/, draft the change in /drafts/, check it with "
    "validate_fix until it is VALID, then propose it with propose_fix_pr. Change only the keys "
    "the fix needs. Return the PR URL, or the reason no PR was opened."
)


def context_files() -> dict[str, str]:
    """Store key -> content. Keys are relative to /agent/ (the route prefix is stripped)."""
    return {"/" + f.relative_to(CONTEXT_DIR).as_posix(): f.read_text() for f in sorted(CONTEXT_DIR.rglob("*.md"))}


def chat_model(name: str) -> ChatOpenAI:
    fallbacks = [m for m in os.environ.get("MODEL_FALLBACKS", "").split(",") if m]
    return ChatOpenAI(
        model=name,
        api_key=os.environ["OPENROUTER_API_KEY"],
        base_url="https://openrouter.ai/api/v1",
        timeout=120,
        max_retries=2,
        extra_body={"models": [name, *fallbacks]} if fallbacks else None,
    )


def build_agent(model, fix_model, tools: list):
    """Pure wiring, no I/O: unit-tested with a fake model."""
    readonly_repo = [FilesystemPermission(operations=["write"], paths=["/agent/**", "/gitops/**"], mode="deny")]
    return create_deep_agent(
        model=model,
        tools=[*tools, recent_changes],
        system_prompt=SYSTEM_PROMPT,
        memory=["/agent/AGENTS.md"],
        skills=["/agent/skills/"],
        response_format=ToolStrategy(DiagnosisResult),
        backend=CompositeBackend(
            default=StateBackend(),
            routes={
                "/agent/": StoreBackend(namespace=lambda _rt: CONTEXT_NS),
                "/memories/": StoreBackend(namespace=lambda _rt: MEMORY_NS),
                # Root is the git-sync volume, not its `main` symlink: FilesystemBackend resolves
                # its root once, so pointing at the symlink would pin the first commit forever.
                "/gitops/": FilesystemBackend(root_dir=GITOPS_ROOT, virtual_mode=True),
            },
        ),
        permissions=readonly_repo,
        subagents=[{
            "name": "fix-agent",
            "description": ("Proposes the Helm values fix as a PR on sre-gitops once the root cause "
                            "is a value in one of the fixable charts: pulsar, voyager (charts/apps/) or "
                            "grafana, loki, prometheus, fluent-bit (charts/infra/). Give it the chart, "
                            "file, the exact key path and old -> new value, and the evidence. A human "
                            "approves the PR."),
            "system_prompt": FIX_AGENT_PROMPT,
            "model": fix_model,
            "tools": [validate_fix_tool, propose_fix_pr],
            "skills": ["/agent/fix-skills/"],
            # A subagent's permissions REPLACE the parent's (docs): read-only except its
            # scratch drafts (thread state). First matching rule wins.
            "permissions": [FilesystemPermission(operations=["write"], paths=["/drafts/**"], mode="allow"),
                            FilesystemPermission(operations=["write"], paths=["/**"], mode="deny")],
            "interrupt_on": {"propose_fix_pr": {"allowed_decisions": ["approve", "edit", "reject"],
                                                "description": describe_fix}},
        }],
    )


_graph = None


async def make_graph(config: RunnableConfig, runtime: ServerRuntime):
    """Graph factory; the server calls it per run, so build once per process."""
    global _graph
    if _graph is None:
        # ponytail: upserts only; a deleted skill lingers in the store until removed by hand
        for key, content in (await asyncio.to_thread(context_files)).items():
            await runtime.store.aput(CONTEXT_NS, key, create_file_data(content))
        model = os.environ["MODEL"]
        _graph = build_agent(chat_model(model), chat_model(os.environ.get("FIX_MODEL") or model), await load_tools())
    return _graph
