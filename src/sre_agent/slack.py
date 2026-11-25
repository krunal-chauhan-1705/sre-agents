"""Slack incident threads and approvals (spec §8.3). Runs on the API pod only (AS-05).

Socket Mode is outbound, so the cluster needs no public URL. Without SLACK_SOCKET_MODE=true and
both tokens every function here is a no-op (IF-07), and approvals happen in Agent Chat UI.
"""
import json
import logging
import os

from langgraph_sdk import get_client

log = logging.getLogger(__name__)
ASSISTANT_ID = "sre_agent"
MAX_DIFF_LINES = 40  # SL-03
_app = None  # slack_bolt AsyncApp once start() connected; None means Slack is off


def configured() -> bool:
    return (os.environ.get("SLACK_SOCKET_MODE") == "true"
            and all(os.environ.get(k) for k in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_CHANNEL_ID")))


def is_approver(user_id: str) -> bool:
    """SL-04: only listed Slack member IDs may approve or reject."""
    return user_id in {u.strip() for u in os.environ.get("SLACK_APPROVER_IDS", "").split(",") if u.strip()}


def chat_ui_link(thread_id: str) -> str:
    base = os.environ.get("CHAT_UI_URL", "").rstrip("/")
    return f"<{base}/?threadId={thread_id}|Open in Chat UI>" if base else ""


def pending_interrupt(thread: dict) -> dict | None:
    """The pending HITL interrupt (propose_fix_pr approval) on a thread, if any (IF-08)."""
    for items in (thread.get("interrupts") or {}).values():
        for it in items:
            if isinstance(it.get("value"), dict) and it["value"].get("action_requests"):
                return it
    return None


def approval_blocks(thread_id: str, interrupt: dict) -> list[dict]:
    """SL-03: file, diff (40 lines), PR title (first line of the description), buttons, Chat UI link."""
    req = interrupt["value"]["action_requests"][0]
    lines = req.get("description", "").replace("```diff", "```").splitlines()
    shown = "\n".join(lines[:MAX_DIFF_LINES])
    if len(lines) > MAX_DIFF_LINES:
        shown += f"\n… {len(lines) - MAX_DIFF_LINES} more lines in Chat UI"
        if shown.count("```") % 2:  # close a code fence the cut left open
            shown += "\n```"
    value = json.dumps({"thread_id": thread_id, "interrupt_id": interrupt.get("id")})
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn",
                                     "text": f":raised_hand: *Approval needed* for `{req['args'].get('file_path', '?')}`"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": shown[:2900]}},  # Slack caps a section at 3000
        {"type": "actions", "block_id": "decision", "elements": [
            {"type": "button", "style": "primary", "text": {"type": "plain_text", "text": "Approve"},
             "action_id": "approve_fix", "value": value},
            {"type": "button", "style": "danger", "text": {"type": "plain_text", "text": "Reject"},
             "action_id": "reject_fix", "value": value},
        ]},
    ]
    if link := chat_ui_link(thread_id):
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"{link} to edit the change"}]})
    return blocks


def decided_blocks(blocks: list[dict], note: str) -> list[dict]:
    """The card after a decision: buttons replaced by who decided (SL-05)."""
    return [b for b in blocks if b.get("type") != "actions"] + [
        {"type": "context", "elements": [{"type": "mrkdwn", "text": note}]}]


def result_text(thread: dict, run_status: str | None) -> str:
    """IF-08: what a finished (not interrupted) run posts to the incident thread."""
    if run_status in ("error", "timeout"):
        return f":x: The run ended with *{run_status}*. Check the trace or Chat UI."
    r = (thread.get("values") or {}).get("structured_response") or {}
    if not r:
        return ":grey_question: The run finished without a diagnosis."
    pr = f"\n*PR:* {r['pr_url']}" if r.get("pr_url") else ""
    return (f":mag: *Diagnosis* ({r.get('confidence', '?')} confidence)\n{r.get('summary', '')}\n"
            f"*Root cause:* {r.get('root_cause', '')}{pr}")


async def post_alert(thread_id: str, text: str, slack_ts: str | None) -> None:
    """SL-02: a new incident gets a root message (its ts saved as thread metadata `slack_ts`);
    a repeat of the same alert group replies in the existing Slack thread."""
    if _app is None:
        return
    channel = os.environ["SLACK_CHANNEL_ID"]
    try:
        if slack_ts:
            await _app.client.chat_postMessage(channel=channel, thread_ts=slack_ts, text=f":repeat: Firing again\n{text}")
            return
        link = chat_ui_link(thread_id)
        r = await _app.client.chat_postMessage(channel=channel, text=f":rotating_light: {text}" + (f"\n{link}" if link else ""))
        await get_client().threads.update(thread_id, metadata={"slack_ts": r["ts"]})
    except Exception:  # noqa: BLE001 - Slack trouble must never block the alert run
        log.exception("Slack: could not post alert for thread %s", thread_id)


async def notify_run(thread_id: str, run_status: str | None) -> None:
    """IF-08: called from /agent-events when a webhook run ends."""
    if _app is None:
        return
    try:
        thread = await get_client().threads.get(thread_id)
        ts = (thread.get("metadata") or {}).get("slack_ts")
        if not ts:  # SL-07: Chat UI threads have no Slack thread
            return
        it = pending_interrupt(thread)
        if it:
            await _app.client.chat_postMessage(channel=os.environ["SLACK_CHANNEL_ID"], thread_ts=ts,
                                               text="Approval needed", blocks=approval_blocks(thread_id, it))
        else:
            await _app.client.chat_postMessage(channel=os.environ["SLACK_CHANNEL_ID"], thread_ts=ts,
                                               text=result_text(thread, run_status))
    except Exception:  # noqa: BLE001
        log.exception("Slack: could not report run on thread %s", thread_id)


async def decide(thread_id: str, interrupt_id: str, decision: dict) -> tuple[str | None, dict | None]:
    """Resume the paused run with one decision. Returns (refusal, interrupt).

    ponytail: strict_resume (SL-05/06) is not in langgraph-api 0.18.6 (/info lacks it), so a
    stale click is caught by re-reading the thread, and a racing second click by
    multitask_strategy="reject" (409 while the first resume runs). Switch to strict_resume=True
    when /info reports it.
    """
    client = get_client()
    thread = await client.threads.get(thread_id)
    it = pending_interrupt(thread)
    if thread.get("status") != "interrupted" or it is None or it.get("id") != interrupt_id:
        return "already decided", it
    try:
        await client.runs.create(thread_id, ASSISTANT_ID, command={"resume": {"decisions": [decision]}},
                                 webhook=os.environ.get("AGENT_EVENTS_URL"), multitask_strategy="reject")
    except Exception as e:  # noqa: BLE001
        if getattr(getattr(e, "response", None), "status_code", None) == 409:
            return "already decided", it
        raise
    return None, it


def build_app(async_app_cls):
    app = async_app_cls(token=os.environ["SLACK_BOT_TOKEN"])

    async def refuse(client, channel, user, text):
        await client.chat_postEphemeral(channel=channel, user=user, text=text)

    @app.action("approve_fix")
    async def on_approve(ack, body, client):
        await ack()
        user, channel, ts = body["user"]["id"], body["channel"]["id"], body["message"]["ts"]
        if not is_approver(user):
            return await refuse(client, channel, user, "You are not an approver for sre-agent.")
        v = json.loads(body["actions"][0]["value"])
        refusal, it = await decide(v["thread_id"], v["interrupt_id"], {"type": "approve"})
        if refusal:
            return await refuse(client, channel, user, "This fix was already decided.")
        await client.chat_update(channel=channel, ts=ts, text="Approved",
                                 blocks=decided_blocks(approval_blocks(v["thread_id"], it),
                                                       f":white_check_mark: Approved by <@{user}>. Opening the PR…"))

    @app.action("reject_fix")
    async def on_reject(ack, body, client):
        await ack()
        user, channel = body["user"]["id"], body["channel"]["id"]
        if not is_approver(user):
            return await refuse(client, channel, user, "You are not an approver for sre-agent.")
        meta = {**json.loads(body["actions"][0]["value"]), "channel": channel, "ts": body["message"]["ts"]}
        await client.views_open(trigger_id=body["trigger_id"], view={
            "type": "modal", "callback_id": "reject_fix", "private_metadata": json.dumps(meta),
            "title": {"type": "plain_text", "text": "Reject fix"},
            "submit": {"type": "plain_text", "text": "Reject"},
            "blocks": [{"type": "input", "block_id": "reason", "optional": True,
                        "label": {"type": "plain_text", "text": "Reason (sent to the agent)"},
                        "element": {"type": "plain_text_input", "action_id": "text", "multiline": True}}],
        })

    @app.view("reject_fix")
    async def on_reject_submit(ack, body, view, client):
        await ack()
        m, user = json.loads(view["private_metadata"]), body["user"]["id"]
        reason = view["state"]["values"]["reason"]["text"].get("value") or "Rejected in Slack, no reason given."
        refusal, it = await decide(m["thread_id"], m["interrupt_id"], {"type": "reject", "message": reason})
        if refusal:
            return await refuse(client, m["channel"], user, "This fix was already decided.")
        await client.chat_update(channel=m["channel"], ts=m["ts"], text="Rejected",
                                 blocks=decided_blocks(approval_blocks(m["thread_id"], it),
                                                       f":no_entry: Rejected by <@{user}>: {reason}"))

    return app


async def start():
    """SL-01: connect Socket Mode from the custom app lifespan. Returns the handler to close."""
    global _app
    if not configured():
        log.info("Slack disabled (needs SLACK_SOCKET_MODE=true, SLACK_BOT_TOKEN, SLACK_APP_TOKEN, SLACK_CHANNEL_ID)")
        return None
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    from slack_bolt.async_app import AsyncApp

    _app = build_app(AsyncApp)
    handler = AsyncSocketModeHandler(_app, os.environ["SLACK_APP_TOKEN"])
    await handler.connect_async()
    log.info("Slack Socket Mode connected")
    return handler
