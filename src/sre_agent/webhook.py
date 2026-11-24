"""Custom routes on the Agent Server (langgraph.json http.app). They run on the API pod."""
import logging
import os
import uuid

from langgraph_sdk import get_client
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

log = logging.getLogger(__name__)
ASSISTANT_ID = "sre_agent"
MAX_ALERTS = 5


def thread_id_for(group_key: str) -> str:
    """One thread per Alertmanager group (IF-02): repeats of the same group land together."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, group_key))


def render_alert(payload: dict) -> str:
    """Alertmanager webhook v4 -> the task message for the agent (IF-06)."""
    common = payload.get("commonLabels", {})
    lines = [f"Alertmanager group {payload.get('status', '?').upper()}: "
             f"{common.get('alertname', '?')} in namespace {common.get('namespace', '?')}."]
    alerts = payload.get("alerts", [])
    for a in alerts[:MAX_ALERTS]:
        lb, an = a.get("labels", {}), a.get("annotations", {})
        where = "/".join(x for x in (lb.get("pod") or lb.get("deployment") or lb.get("kubernetes_container_name"),
                                     lb.get("container")) if x)
        text = " - ".join(x for x in (an.get("summary"), an.get("description")) if x)
        lines.append(f"- {lb.get('alertname', '?')} [{a.get('status', '?')}] {where or 'n/a'} since {a.get('startsAt', '?')}: {text}")
    if len(alerts) > MAX_ALERTS:
        lines.append(f"- ... and {len(alerts) - MAX_ALERTS} more alerts in this group")
    lines.append("Diagnose the root cause. If it is a Helm values problem in pulsar or voyager, "
                 "hand the fix to fix-agent.")
    return "\n".join(lines)


async def alertmanager(request: Request) -> JSONResponse:
    payload = await request.json()
    if payload.get("status") != "firing":  # IF-01
        return JSONResponse({"skipped": "not firing"})
    group_key = payload["groupKey"]
    tid = thread_id_for(group_key)
    client = get_client()  # in-process connection to this server
    try:
        thread = await client.threads.get(tid)
    except Exception as e:  # noqa: BLE001 - 404 means a new incident
        if getattr(getattr(e, "response", None), "status_code", None) != 404:
            raise
        thread = None
    if thread and thread["status"] in ("busy", "interrupted"):  # IF-03: never feed a pending approval
        return JSONResponse({"skipped": thread["status"], "thread_id": tid})
    common = payload.get("commonLabels", {})
    run = await client.runs.create(
        tid, ASSISTANT_ID,
        input={"messages": [{"role": "user", "content": render_alert(payload)}]},
        if_not_exists="create",
        multitask_strategy="reject",
        webhook=os.environ.get("AGENT_EVENTS_URL"),
        metadata={"source": "alertmanager", "alertname": common.get("alertname"),
                  "namespace": common.get("namespace"), "group_key": group_key},
        config={"recursion_limit": 80},  # AG-10 / BUD-02
    )
    return JSONResponse({"thread_id": tid, "run_id": run["run_id"]}, status_code=202)


async def agent_events(request: Request) -> JSONResponse:
    """Run-completion webhook (IF-07). Slack (Phase 6) posts from here; until then a no-op."""
    body = await request.json()
    log.info("run %s on thread %s finished: %s", body.get("run_id"), body.get("thread_id"), body.get("status"))
    return JSONResponse({"ok": True})


app = Starlette(routes=[
    Route("/alertmanager", alertmanager, methods=["POST"]),
    Route("/agent-events", agent_events, methods=["POST"]),
])
