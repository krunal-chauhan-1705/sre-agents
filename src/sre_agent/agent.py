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
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.structured_output import ToolStrategy
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph_sdk.runtime import ServerRuntime
from pydantic import BaseModel, Field

from sre_agent.mcp import load_tools
from sre_agent.tools import GITOPS_DIR, describe_fix, propose_fix_pr, recent_changes, validate_fix_tool

CONTEXT_NS = ("sre_agent", "context")
MEMORY_NS = ("sre_agent", "memories")
CONTEXT_DIR = pathlib.Path(__file__).parent / "context"


class GitSyncBackend(FilesystemBackend):
    """Read-only view of the git-sync checkout whose root follows git-sync's `main` symlink.

    FilesystemBackend resolves its root once (`self.cwd`). Rooted at the symlink it would pin
    the first commit; rooted at the volume it returns resolved `.worktrees/<sha>/` paths that
    go stale on the next sync. Re-resolving per call keeps paths `/charts/...` and current.
    """

    # ponytail: overrides the backend's `cwd` attribute (deepagents 0.7.39). Covered by
    # test_gitops_mount_follows_git_sync_symlink_swaps; re-check on deepagents upgrades.
    @property
    def cwd(self):
        return GITOPS_DIR.resolve()

    @cwd.setter
    def cwd(self, _value):
        pass


class FixProposal(BaseModel):
    app: Literal["pulsar", "voyager", "grafana", "loki", "prometheus", "fluent-bit"]
    file_path: str
    change: str = Field(description='one line, e.g. "faultInjection.failEvery: 3 -> 0"')


class DiagnosisResult(BaseModel):
    """Your final answer. Call it exactly once, as soon as the evidence supports one root cause
    (or answers the person's question). Calling it ends the run."""

    summary: str = Field(description="1-2 sentences")
    root_cause: str
    confidence: Literal["low", "medium", "high"]
    evidence: list[str] = Field(description="each item cites its source: tool + query/resource")
    affected: list[str] = Field(description='e.g. "default/deployment/pulsar"')
    suspected_change: str | None = Field(description="commit sha + file, if found")
    fix: FixProposal | None
    pr_url: str | None = Field(description="ONLY the URL returned by propose_fix_pr, else null")


SYSTEM_PROMPT = (
    "You are sre_agent, the first responder for a Kubernetes cluster managed by ArgoCD from the "
    "sre-gitops repo. Follow your AGENTS.md memory: an alert gets the full workflow, a question "
    "from a person gets a direct answer with as few tool calls as possible."
)

MODEL_CALL_DEADLINE = 120  # seconds for one whole model call, streaming included


class ModelDeadline(AgentMiddleware):
    """Caps each model call's wall-clock time, then retries once.

    ChatOpenAI's `timeout` is httpx's per-read timeout: it resets on every byte, so a free-model
    stream that stays open without finishing hung a run until BG_JOB_TIMEOUT (15 min).
    """

    async def awrap_model_call(self, request, handler):
        try:
            return await asyncio.wait_for(handler(request), MODEL_CALL_DEADLINE)
        except TimeoutError:
            return await asyncio.wait_for(handler(request), MODEL_CALL_DEADLINE)


class PlainToolText(AgentMiddleware):
    """MCP tools return content blocks ([{"type": "text", "text": ...}]); Agent Chat UI renders
    those as "[object Object]". Store text-only results as one plain string instead."""

    async def awrap_tool_call(self, request, handler):
        msg = await handler(request)
        content = getattr(msg, "content", None)
        if isinstance(content, list) and all(isinstance(b, dict) and b.get("type") == "text" for b in content):
            msg.content = "\n".join(b["text"] for b in content)
        return msg

FIX_AGENT_PROMPT = (
    "You turn a finished diagnosis into ONE minimal Helm values change. Follow your helm-fix-pr "
    "skill: read the current file under /gitops/, draft the change in /drafts/, check it with "
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
        middleware=[ModelDeadline(), PlainToolText()],
        backend=CompositeBackend(
            default=StateBackend(),
            routes={
                "/agent/": StoreBackend(namespace=lambda _rt: CONTEXT_NS),
                "/memories/": StoreBackend(namespace=lambda _rt: MEMORY_NS),
                # sre-gitops main, mounted read-only by the git-sync sidecar (see GitSyncBackend).
                "/gitops/": GitSyncBackend(root_dir=GITOPS_DIR, virtual_mode=True),
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
            "middleware": [ModelDeadline()],
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
