"""Pure-function and wiring tests. No network, no real model (BUD-01)."""
import asyncio
import json

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from sre_agent import gitops, tools
from deepagents.backends.utils import create_file_data

from sre_agent.agent import CONTEXT_NS, DiagnosisResult, build_agent, context_files
from sre_agent.tools import fix_diff, validate_fix
from sre_agent.webhook import render_alert, thread_id_for

VALUES = "image:\n  tag: latest\nfaultInjection:\n  failEvery: 3\n"
PAYLOAD = {
    "status": "firing", "groupKey": '{}:{alertname="AppErrorLogs", namespace="default"}',
    "commonLabels": {"alertname": "AppErrorLogs", "namespace": "default"},
    "alerts": [{"status": "firing", "startsAt": "2026-11-26T10:00:00Z",
                "labels": {"alertname": "AppErrorLogs", "kubernetes_container_name": "voyager"},
                "annotations": {"summary": "voyager logged 12 errors/5xx in 5m"}}] * 7,
}


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "charts/apps/pulsar").mkdir(parents=True)
    (tmp_path / "charts/apps/pulsar/values.yaml").write_text(VALUES)
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


class FakeToolModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def make_graph(monkeypatch, *messages):
    async def no_refresh():
        return None
    monkeypatch.setattr(gitops, "refresh_checkout", no_refresh)
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
    assert intr.value["review_configs"][0]["allowed_decisions"] == ["approve", "edit", "reject"]
