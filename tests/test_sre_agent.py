"""Pure-function and wiring tests. No network, no real model (BUD-01)."""
import asyncio
import json
import shutil

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from sre_agent import tools
from deepagents.backends.utils import create_file_data

from sre_agent.agent import CONTEXT_NS, DiagnosisResult, build_agent, context_files
from sre_agent.tools import check_fix, fix_diff, validate_fix
from sre_agent.webhook import render_alert, thread_id_for

VALUES = "image:\n  tag: latest\nfaultInjection:\n  failEvery: 3\n"
PAYLOAD = {
    "status": "firing", "groupKey": '{}:{alertname="AppErrorLogs", namespace="default"}',
    "commonLabels": {"alertname": "AppErrorLogs", "namespace": "default"},
    "alerts": [{"status": "firing", "startsAt": "2026-11-26T10:00:00Z",
                "labels": {"alertname": "AppErrorLogs", "kubernetes_container_name": "voyager"},
                "annotations": {"summary": "voyager logged 12 errors/5xx in 5m"}}] * 7,
}


DEPLOYMENT = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ .Chart.Name }}
spec:
  template:
    spec:
      containers:
        - name: {{ .Chart.Name }}
          image: "pulsar:{{ .Values.image.tag }}"
          env:
            - name: PULSE_FAIL_EVERY
              value: {{ .Values.faultInjection.failEvery | quote }}
"""


@pytest.fixture
def repo(tmp_path):
    for d in ("charts/apps/pulsar", "charts/infra/loki", "charts/infra/mcp-grafana", "charts/infra/sre-agent"):
        (tmp_path / d).mkdir(parents=True)
        (tmp_path / d / "values.yaml").write_text(VALUES)
    chart = tmp_path / "charts/apps/pulsar"
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: pulsar\nversion: 0.1.0\n")
    (chart / "templates").mkdir()
    (chart / "templates/deployment.yaml").write_text(DEPLOYMENT)
    return tmp_path


def test_thread_id_is_stable_per_group():
    assert thread_id_for("g1") == thread_id_for("g1") != thread_id_for("g2")


def test_render_alert_is_bounded_and_complete():
    text = render_alert(PAYLOAD)
    assert "AppErrorLogs in namespace default" in text
    assert "voyager" in text and "12 errors" in text
    assert text.count("\n- AppErrorLogs") == 5 and "2 more alerts" in text


@pytest.mark.parametrize("app,path,content,error", [
    ("pulsar", "charts/apps/pulsar/values.yaml", VALUES.replace("3", "0"), None),
    ("nginx", "charts/apps/pulsar/values.yaml", "a: 1", "app must be"),
    ("pulsar", "charts/apps/voyager/values.yaml", "a: 1", "under charts/apps/pulsar/"),
    ("pulsar", "charts/apps/pulsar/../../infra/x.yaml", "a: 1", "under charts/apps/pulsar/"),
    ("pulsar", "charts/infra/grafana/values.yaml", "a: 1", "under charts/apps/pulsar/"),
    ("loki", "charts/infra/loki/values.yaml", VALUES.replace("3", "0"), None),
    ("loki", "charts/apps/pulsar/values.yaml", "a: 1", "under charts/infra/loki/"),
    ("mcp-grafana", "charts/infra/mcp-grafana/values.yaml", "a: 1", "app must be one of"),
    ("sre-agent", "charts/infra/sre-agent/values.yaml", "a: 1", "app must be one of"),
    ("kubernetes-mcp", "charts/infra/kubernetes-mcp/values.yaml", "a: 1", "app must be one of"),
    ("pulsar", "charts/apps/pulsar/templates.txt", "a: 1", "under charts/apps/pulsar/"),
    ("pulsar", "charts/apps/pulsar/missing.yaml", "a: 1", "does not exist"),
    ("pulsar", "charts/apps/pulsar/values.yaml", "a: [1", "not valid YAML"),
    ("pulsar", "charts/apps/pulsar/values.yaml", VALUES, "identical"),
])
def test_validate_fix(repo, app, path, content, error):
    result = validate_fix(app, path, content, root=repo)
    assert (result is None) if error is None else (error in result)


def test_fix_diff_shows_the_change(repo):
    diff = fix_diff("charts/apps/pulsar/values.yaml", VALUES.replace("failEvery: 3", "failEvery: 0"), root=repo)
    assert "-  failEvery: 3" in diff and "+  failEvery: 0" in diff


needs_helm = pytest.mark.skipif(not shutil.which("helm"), reason="helm not installed")
PULSAR_VALUES = "charts/apps/pulsar/values.yaml"


@needs_helm
def test_check_fix_shows_rendered_change(repo):
    out = check_fix("pulsar", PULSAR_VALUES, VALUES.replace("failEvery: 3", "failEvery: 0"), root=repo)
    assert "~ Deployment/pulsar" in out and "value: '0'" in out


@needs_helm
def test_check_fix_rejects_change_with_no_effect(repo):
    # The classic mistake: a key the templates never read (e.g. missing subchart nesting).
    with pytest.raises(ValueError, match="NO EFFECT"):
        check_fix("pulsar", PULSAR_VALUES, VALUES + "singleBinary:\n  replicas: 2\n", root=repo)


@needs_helm
def test_check_fix_rejects_change_that_does_not_render(repo):
    with pytest.raises(ValueError, match="does not render"):
        check_fix("pulsar", PULSAR_VALUES, "image: null\nfaultInjection:\n  failEvery: 0\n", root=repo)


class FakeToolModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def make_graph(monkeypatch, *messages):
    model = FakeToolModel(messages=iter(messages))
    graph = build_agent(model, model, tools=[])
    graph.store = InMemoryStore()  # the Agent Server injects its Postgres store + checkpointer
    graph.checkpointer = InMemorySaver()
    for key, content in context_files().items():
        graph.store.put(CONTEXT_NS, key, create_file_data(content))
    return graph.with_config({"configurable": {"thread_id": "t1"}})


def test_graph_runs_to_structured_diagnosis(monkeypatch):
    result = {"summary": "pulsar fault injection on", "root_cause": "failEvery: 3", "confidence": "high",
              "evidence": ["loki: 12 tracebacks"], "affected": ["default/deployment/pulsar"],
              "suspected_change": None, "fix": None, "pr_url": None}
    graph = make_graph(monkeypatch, AIMessage(content="", tool_calls=[
        {"name": "DiagnosisResult", "args": result, "id": "call-1"}]))
    out = asyncio.run(graph.ainvoke({"messages": [{"role": "user", "content": render_alert(PAYLOAD)}]}))
    assert out["structured_response"] == DiagnosisResult(**result)


def test_fix_pr_pauses_for_human_with_diff(monkeypatch, repo):
    monkeypatch.setattr(tools, "GITOPS_DIR", repo)
    new = VALUES.replace("failEvery: 3", "failEvery: 0")
    graph = make_graph(
        monkeypatch,
        AIMessage(content="", tool_calls=[{"name": "task", "id": "c1", "args": {
            "subagent_type": "fix-agent", "description": "pulsar failEvery 3 -> 0"}}]),
        AIMessage(content="", tool_calls=[{"name": "propose_fix_pr", "id": "c2", "args": {
            "app": "pulsar", "file_path": "charts/apps/pulsar/values.yaml", "new_content": new,
            "title": "fix(pulsar): disable fault injection", "body": "b"}}]),
    )
    out = asyncio.run(graph.ainvoke({"messages": [{"role": "user", "content": "alert"}]}))
    (intr,) = out["__interrupt__"]
    (req,) = intr.value["action_requests"]
    assert req["name"] == "propose_fix_pr"
    assert "+  failEvery: 0" in req["description"]
    if shutil.which("helm"):
        assert "~ Deployment/pulsar" in req["description"]  # the human sees the rendered change
    assert intr.value["review_configs"][0]["allowed_decisions"] == ["approve", "edit", "reject"]


def test_gitops_mount_follows_git_sync_symlink_swaps(tmp_path, monkeypatch):
    """git-sync layout: /git/.worktrees/<sha>/ + /git/main -> current. Reads must see new commits."""
    from sre_agent.agent import GitSyncBackend
    for sha, every in (("aaa", 3), ("bbb", 0)):
        d = tmp_path / ".worktrees" / sha / "charts/apps/pulsar"
        d.mkdir(parents=True)
        (d / "values.yaml").write_text(f"faultInjection:\n  failEvery: {every}\n")
    (tmp_path / "main").symlink_to(".worktrees/aaa")
    monkeypatch.setattr(tools, "GITOPS_DIR", tmp_path / "main")
    monkeypatch.setattr("sre_agent.agent.GITOPS_DIR", tmp_path / "main")
    backend = GitSyncBackend(root_dir=tmp_path / "main", virtual_mode=True)  # built once, like the graph

    def read() -> str:
        return backend.read("/charts/apps/pulsar/values.yaml").file_data["content"]
    assert "failEvery: 3" in read()
    assert [e["path"] for e in backend.ls("/charts/apps").entries] == ["/charts/apps/pulsar/"]  # no sha paths
    try:
        backend.read("/../../.worktrees/aaa/charts/apps/pulsar/values.yaml")
        raise AssertionError("traversal out of the checkout must fail")
    except ValueError:
        pass

    (tmp_path / "main.tmp").symlink_to(".worktrees/bbb")  # git-sync's atomic swap
    (tmp_path / "main.tmp").replace(tmp_path / "main")
    assert "failEvery: 0" in read()
    assert "identical" in validate_fix("pulsar", "charts/apps/pulsar/values.yaml", "faultInjection:\n  failEvery: 0\n")


def test_model_deadline_retries_once_then_gives_up(monkeypatch):
    import asyncio
    import sre_agent.agent as agent_mod

    monkeypatch.setattr(agent_mod, "MODEL_CALL_DEADLINE", 0.05)
    calls = []

    async def hangs_first(request):
        calls.append(request)
        if len(calls) == 1:
            await asyncio.sleep(1)
        return "ok"

    assert asyncio.run(agent_mod.ModelDeadline().awrap_model_call("req", hangs_first)) == "ok"
    assert len(calls) == 2

    async def always_hangs(request):
        await asyncio.sleep(1)

    with pytest.raises(TimeoutError):
        asyncio.run(agent_mod.ModelDeadline().awrap_model_call("req", always_hangs))


def test_plain_tool_text_flattens_mcp_text_blocks():
    import asyncio
    from langchain_core.messages import ToolMessage
    from sre_agent.agent import PlainToolText

    async def mcp_tool(_req):
        return ToolMessage(content=[{"type": "text", "text": "a"}, {"type": "text", "text": "b"}], tool_call_id="1")

    async def plain_tool(_req):
        return ToolMessage(content="already text", tool_call_id="2")

    assert asyncio.run(PlainToolText().awrap_tool_call(None, mcp_tool)).content == "a\nb"
    assert asyncio.run(PlainToolText().awrap_tool_call(None, plain_tool)).content == "already text"
