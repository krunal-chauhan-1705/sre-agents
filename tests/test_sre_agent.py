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


def test_thread_id_is_per_incident():
    again = {**PAYLOAD, "alerts": [*PAYLOAD["alerts"], {**PAYLOAD["alerts"][0], "startsAt": "2026-11-26T10:05:00Z"}]}
    later = {**PAYLOAD, "alerts": [{**PAYLOAD["alerts"][0], "startsAt": "2026-11-27T09:00:00Z"}]}
    other = {**PAYLOAD, "groupKey": PAYLOAD["groupKey"] + "x"}
    assert thread_id_for(PAYLOAD) == thread_id_for(again)  # repeat, alert added: same incident
    assert thread_id_for(later) != thread_id_for(PAYLOAD)  # resolved, fired again: new incident
    assert thread_id_for(other) != thread_id_for(PAYLOAD)


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


def _hitl_thread(status="interrupted"):
    desc = "Open a PR on sre-gitops: pulsar: stop fault injection\n\nValues change:\n```diff\n" + \
           "\n".join(f"+ line {i}" for i in range(60)) + "\n```"
    return {"status": status, "metadata": {"slack_ts": "1.2"}, "interrupts": {"task-1": [{"id": "intr-1", "value": {
        "action_requests": [{"name": "propose_fix_pr", "args": {"file_path": "charts/apps/pulsar/values.yaml"},
                             "description": desc}],
        "review_configs": [{"action_name": "propose_fix_pr", "allowed_decisions": ["approve", "edit", "reject"]}]}}]}}


def test_slack_approval_card(monkeypatch):
    import json
    from sre_agent import slack

    monkeypatch.setenv("CHAT_UI_URL", "http://localhost:3000")
    it = slack.pending_interrupt(_hitl_thread())
    blocks = slack.approval_blocks("t-1", it)
    assert "charts/apps/pulsar/values.yaml" in blocks[0]["text"]["text"]
    body = blocks[1]["text"]["text"]
    assert "more lines in Chat UI" in body and body.count("```") % 2 == 0  # cut at 40 lines, fence closed
    buttons = {b["action_id"]: json.loads(b["value"]) for b in blocks[2]["elements"]}
    assert buttons["approve_fix"] == {"thread_id": "t-1", "interrupt_id": "intr-1"}
    assert "threadId=t-1" in blocks[3]["elements"][0]["text"]
    done = slack.decided_blocks(blocks, "Approved by <@U1>")
    assert all(b["type"] != "actions" for b in done) and done[-1]["elements"][0]["text"] == "Approved by <@U1>"


def test_slack_approvers_and_results(monkeypatch):
    from sre_agent import slack

    monkeypatch.setenv("SLACK_APPROVER_IDS", "U1, U2")
    assert slack.is_approver("U2") and not slack.is_approver("U3")
    assert slack.pending_interrupt({"interrupts": {}}) is None
    thread = {"values": {"structured_response": {"summary": "s", "root_cause": "rc", "confidence": "high",
                                                 "pr_url": "https://github.com/x/pull/1"}}}
    assert "<https://github.com/x/pull/1|#1 on GitHub>" in slack.result_text(thread, "success")
    assert "error" in slack.result_text({}, "error")


def test_slack_stale_click_is_refused_without_resuming(monkeypatch):
    import asyncio
    from sre_agent import slack

    class FakeClient:
        def __init__(self, thread):
            self.threads, self.runs, self.created = self, self, []
            self._thread = thread

        async def get(self, _tid):
            return self._thread

        async def create(self, *a, **kw):
            self.created.append(kw)

    stale = FakeClient(_hitl_thread(status="idle"))  # already decided in Chat UI
    monkeypatch.setattr(slack, "get_client", lambda: stale)
    assert asyncio.run(slack.decide("t-1", "intr-1", {"type": "approve"}))[0] == "already decided"
    assert stale.created == []

    live = FakeClient(_hitl_thread())
    monkeypatch.setattr(slack, "get_client", lambda: live)
    assert asyncio.run(slack.decide("t-1", "intr-1", {"type": "approve"}))[0] is None
    assert live.created[0]["command"] == {"resume": {"decisions": [{"type": "approve"}]}}
    assert live.created[0]["multitask_strategy"] == "reject"


def test_slack_is_off_without_tokens(monkeypatch):
    import asyncio
    from sre_agent import slack

    monkeypatch.setenv("SLACK_SOCKET_MODE", "true")
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    assert asyncio.run(slack.start()) is None and slack._app is None
    asyncio.run(slack.post_alert("t", "x", None))  # no-op, no error (IF-07)


def test_claude_model_uses_effort_native_output_and_fallback(monkeypatch):
    import sre_agent.agent as agent_mod

    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    m = agent_mod.chat_model("claude-haiku-5-5")
    assert m.profile["structured_output"] is True  # -> ProviderStrategy, tool_choice stays auto
    assert m._get_request_payload([("user", "hi")])["output_config"] == {"effort": "medium"}
    fb = agent_mod.chat_model("particle-ai/particle-1:free")
    assert not (fb.profile or {}).get("structured_output")  # -> ToolStrategy for the fallback
    graph = build_agent(m, m, tools=[], fallbacks=[fb])
    assert graph is not None


def test_fix_pending_suppresses_rediagnosis_for_a_while():
    from datetime import datetime, timedelta, timezone

    from sre_agent.webhook import fix_pending
    now = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
    t = {"updated_at": "2026-11-25T11:55:00Z", "values": {"structured_response": {"pr_url": "https://x/pull/1"}}}
    assert fix_pending(t, now)
    assert not fix_pending(t, now + timedelta(minutes=5))  # 10 min after the PR, still firing: re-diagnose
    assert not fix_pending({**t, "values": {"structured_response": {"pr_url": None}}}, now)
    assert not fix_pending(None, now)


def test_fallback_logs_and_switches_but_not_on_config_errors():
    import sre_agent.agent as agent_mod

    class Req:
        def __init__(self, model): self.model = model
        def override(self, model): return Req(model)

    class Err(Exception):
        def __init__(self, code): self.status_code = code

    def handler_failing(code):
        async def h(req):
            if req.model == "primary":
                raise Err(code)
            return req.model
        return h

    fb = agent_mod.Fallback("fallback")
    assert asyncio.run(fb.awrap_model_call(Req("primary"), handler_failing(429))) == "fallback"
    with pytest.raises(Err):
        asyncio.run(fb.awrap_model_call(Req("primary"), handler_failing(401)))


def fake_github(open_prs, pr_files, main_content, calls):
    import base64

    import httpx

    def handle(req):
        calls.append((req.method, req.url.path))
        p = req.url.path
        if p.endswith("/contents/charts/infra/fluent-bit/values.yaml"):
            return httpx.Response(200, json={"content": base64.b64encode(main_content.encode()).decode()})
        if p.endswith("/pulls") and req.method == "GET":
            return httpx.Response(200, json=open_prs)
        if p.endswith("/files"):
            return httpx.Response(200, json=pr_files)
        if p.endswith("/requested_reviewers"):
            return httpx.Response(201, json={})
        if p.endswith("/assignees"):
            return httpx.Response(201, json={})
        return httpx.Response(404)
    return httpx.AsyncClient(base_url="https://api.github.com/repos/o/r", transport=httpx.MockTransport(handle))


def test_duplicate_fix_prs_are_not_opened():
    f = "charts/infra/fluent-bit/values.yaml"
    pr = {"number": 4, "html_url": "https://github.com/o/r/pull/4", "head": {"ref": "sre-agent/fluent-bit-1"}}
    calls = []
    # main already fixed by another incident's PR
    assert "main already has" in asyncio.run(tools.already_handled(fake_github([], [], "fixed\n", calls), f, "fixed\n"))
    # another incident's PR for the same file is still open
    why = asyncio.run(tools.already_handled(fake_github([pr], [{"filename": f}], "broken\n", calls), f, "fixed\n"))
    assert why.startswith("PR #4 already open")
    # unrelated open PR: go ahead
    assert asyncio.run(tools.already_handled(fake_github([pr], [{"filename": "other"}], "broken\n", calls), f, "fixed\n")) is None


def test_reviewer_is_assigned_and_requested_unless_author(monkeypatch):
    monkeypatch.setenv("PR_REVIEWERS", "alice")
    calls = []
    gh = fake_github([], [], "", calls)
    assert "review requested alice" in asyncio.run(tools.add_reviewers(gh, {"number": 5, "user": {"login": "bot"}}))
    calls.clear()
    out = asyncio.run(tools.add_reviewers(gh, {"number": 5, "user": {"login": "Alice"}}))  # PAT owner = reviewer
    assert "assigned alice" in out and not any(p.endswith("/requested_reviewers") for _, p in calls)


def test_priority_drives_the_instructions():
    from sre_agent.webhook import priority
    info = {**PAYLOAD, "alerts": [{**a, "labels": {**a["labels"], "severity": "info"}} for a in PAYLOAD["alerts"]]}
    mixed = {**PAYLOAD, "alerts": [*info["alerts"], {**PAYLOAD["alerts"][0], "labels": {"severity": "critical"}}]}
    assert priority(info) == "info" and priority(mixed) == "critical" and priority({"alerts": []}) == "warning"
    text = render_alert(info)
    assert "Priority: info." in text and "Do NOT delegate to fix-agent" in text
    assert "hand the fix to fix-agent" in render_alert(mixed)


def test_pending_approval_in_another_incident_is_a_duplicate():
    f = "charts/infra/grafana/values.yaml"
    card = {"value": {"action_requests": [{"name": "propose_fix_pr", "args": {"file_path": f}}]}}
    threads = [{"thread_id": "own", "interrupts": {"t": [card]}},
               {"thread_id": "other", "interrupts": {"t": [card]}},
               {"thread_id": "unrelated", "interrupts": {}}]
    assert tools.pending_for(threads, f, "own") == "other"
    assert tools.pending_for(threads[:1], f, "own") is None          # its own card doesn't count
    assert tools.pending_for(threads, "charts/apps/pulsar/values.yaml", "own") is None
    # a card from an incident that began before the file last changed on main is stale
    threads[1]["created_at"] = "2026-11-25T12:35:00Z"
    assert tools.pending_for(threads, f, "own", changed_at="2026-11-26T10:52:57Z") is None
    threads[1]["created_at"] = "2026-11-26T10:57:00Z"
    assert tools.pending_for(threads, f, "own", changed_at="2026-11-26T10:52:57Z") == "other"


def test_validate_fix_reports_duplicates(monkeypatch, repo):
    monkeypatch.setattr(tools, "GITOPS_DIR", repo)

    async def dup(file_path, new_content):
        return "PR #7 already open for x"
    monkeypatch.setattr(tools, "duplicate_of", dup)
    new = VALUES.replace("failEvery: 3", "failEvery: 0")
    out = asyncio.run(tools.validate_fix_tool.ainvoke({"app": "pulsar", "file_path": "charts/apps/pulsar/values.yaml", "new_content": new}))
    assert out.startswith("DUPLICATE: PR #7 already open") and "Do NOT call propose_fix_pr" in out
