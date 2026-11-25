"""Custom routes on the Agent Server (langgraph.json http.app). They run on the API pod."""
import contextlib
import logging
import os
import uuid
from datetime import datetime, timezone

from langgraph_sdk import get_client
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from sre_agent import slack

log = logging.getLogger(__name__)
ASSISTANT_ID = "sre_agent"
MAX_ALERTS = 5
FIX_COOLDOWN_S = 10 * 60  # after a fix PR: time to merge, let ArgoCD sync and the alert resolve


def fix_pending(thread: dict | None, now: datetime) -> bool:
    """The group's last run opened a fix PR recently. Alertmanager keeps notifying a firing group
    (group_interval when its alerts change, repeat_interval otherwise); re-diagnosing before the
    PR can take effect only repeats the same answer. Still firing after the cooldown = the fix
    did not work, which is worth a new diagnosis."""
    pr = (((thread or {}).get("values") or {}).get("structured_response") or {}).get("pr_url")
    if not pr:
        return False
    updated = datetime.fromisoformat(thread["updated_at"].replace("Z", "+00:00"))
    return (now - updated).total_seconds() < FIX_COOLDOWN_S


def thread_id_for(payload: dict) -> str:
    """One thread (and Slack thread) per incident: the alert group plus when it started firing.

    groupKey alone never changes, so every future occurrence would land in the first one's
    thread. A firing alert keeps its startsAt across notifications (repeats stay together);
    after it resolves and fires again it gets a new startsAt, so a new incident.
    """
    started = min((a.get("startsAt", "") for a in payload.get("alerts", [])), default="")
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{payload['groupKey']}|{started}"))


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
    tid = thread_id_for(payload)
    client = get_client()  # in-process connection to this server
    try:
        thread = await client.threads.get(tid)
    except Exception as e:  # noqa: BLE001 - 404 means a new incident
        if getattr(getattr(e, "response", None), "status_code", None) != 404:
            raise
        thread = None
    if thread and thread["status"] in ("busy", "interrupted"):  # IF-03: never feed a pending approval
        return JSONResponse({"skipped": thread["status"], "thread_id": tid})
    if fix_pending(thread, datetime.now(timezone.utc)):
        log.info("alert group %s still firing; fix PR pending, not re-diagnosing", tid)
        return JSONResponse({"skipped": "fix pending", "thread_id": tid})
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
    alert_text = "\n".join(render_alert(payload).splitlines()[:-1])  # without the instruction line
    await slack.post_alert(tid, alert_text, ((thread or {}).get("metadata") or {}).get("slack_ts"))
    return JSONResponse({"thread_id": tid, "run_id": run["run_id"]}, status_code=202)


async def agent_events(request: Request) -> JSONResponse:
    """Run-completion webhook (IF-07/08): approval card or diagnosis in the incident's Slack thread."""
    body = await request.json()
    log.info("run %s on thread %s finished: %s", body.get("run_id"), body.get("thread_id"), body.get("status"))
    await slack.notify_run(body["thread_id"], body.get("status"))
    return JSONResponse({"ok": True})


@contextlib.asynccontextmanager
async def lifespan(_app):
    handler = await slack.start()  # SL-01: no-op unless SLACK_SOCKET_MODE=true (API pod only, AS-05)
    try:
        yield
    finally:
        if handler:
            await handler.close_async()


app = Starlette(routes=[
    Route("/alertmanager", alertmanager, methods=["POST"]),
    Route("/agent-events", agent_events, methods=["POST"]),
], lifespan=lifespan)
