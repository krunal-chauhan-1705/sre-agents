"""The agent's GitHub-facing tools.

propose_fix_pr is the ONLY write path (SPEC §7.3, SEC-02): fixed repo, an allow-list of
charts, YAML files only, behind HITL. The agent's own charts (mcp-grafana, kubernetes-mcp,
sre-agent) are deliberately NOT in the list: it must never be able to widen its own access. The model chooses the content; this code chooses where it
goes. validate_fix renders the chart with `helm template` (offline, fixed argv, never the
model's command) so a change is checked before a human sees it. recent_changes is the
read-only "what changed" lookup for diagnosis.
"""
import asyncio
import base64
import difflib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx
import yaml
from langchain.tools import tool
from langgraph.config import get_config

# The sre-gitops repo is mounted read-only by a git-sync sidecar: GITOPS_ROOT is the volume,
# GITOPS_DIR the symlink git-sync swaps atomically to each new commit of main.
GITOPS_ROOT = Path(os.environ.get("GITOPS_ROOT", "/git"))
GITOPS_DIR = GITOPS_ROOT / "main"
GITHUB_API = "https://api.github.com"


def github_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {os.environ['GITHUB_PAT']}",  # header, never in the URL
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

# chart name -> (chart directory in sre-gitops, namespace ArgoCD deploys it to).
# Only these can be changed by a fix PR. Release name = chart name (apps/<chart>.yaml).
CHARTS = {
    "pulsar": ("charts/apps/pulsar", "default"),
    "voyager": ("charts/apps/voyager", "default"),
    "grafana": ("charts/infra/grafana", "o11y"),
    "loki": ("charts/infra/loki", "o11y"),
    "prometheus": ("charts/infra/prometheus", "o11y"),   # includes Alertmanager
    "fluent-bit": ("charts/infra/fluent-bit", "o11y"),
}
CHART_DIRS = {name: d for name, (d, _ns) in CHARTS.items()}
App = Literal["pulsar", "voyager", "grafana", "loki", "prometheus", "fluent-bit"]


def validate_fix(app: str, file_path: str, new_content: str, root=None) -> str | None:
    """Return an error message, or None when the proposal is allowed (FX-01, FX-02)."""
    root = root or GITOPS_DIR
    if app not in CHART_DIRS:
        return f"app must be one of {', '.join(CHART_DIRS)}, got {app!r}"
    chart_dir = CHART_DIRS[app]
    if ".." in file_path or not re.fullmatch(rf"{re.escape(chart_dir)}/[A-Za-z0-9_./-]+\.ya?ml", file_path):
        return f"file_path must be a YAML file under {chart_dir}/, got {file_path!r}"
    current = root / file_path
    if not current.is_file():
        return f"{file_path} does not exist in the gitops repo"
    try:
        yaml.safe_load(new_content)
    except yaml.YAMLError as e:
        return f"new_content is not valid YAML: {e}"
    if new_content == current.read_text():
        return "new_content is identical to the current file; nothing to change"
    return None


def fix_diff(file_path: str, new_content: str, root=None, max_lines: int = 40) -> str:
    """Unified diff of the repo file vs the proposal, for the approval card."""
    path = (root or GITOPS_DIR) / file_path
    old = path.read_text() if path.is_file() else ""
    lines = list(difflib.unified_diff(old.splitlines(), new_content.splitlines(),
                                      f"a/{file_path}", f"b/{file_path}", lineterm=""))
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... ({len(lines) - max_lines} more lines)"]
    return "\n".join(lines)


def _helm_template(chart: Path, release: str, namespace: str) -> str:
    """Offline render. No cluster, no kubeconfig, no shell; fixed argv; bounded time."""
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(chart.parent),
           "KUBECONFIG": "/dev/null"}
    r = subprocess.run(["helm", "template", release, str(chart), "--namespace", namespace],
                       capture_output=True, text=True, timeout=30, env=env)
    if r.returncode != 0:
        raise ValueError(r.stderr.strip()[-1500:] or "helm template failed")
    return r.stdout


def _objects(rendered: str) -> dict[str, dict]:
    out = {}
    for doc in yaml.safe_load_all(rendered):
        if isinstance(doc, dict) and doc.get("kind"):
            out[f"{doc['kind']}/{doc.get('metadata', {}).get('name', '?')}"] = doc
    return out


def rendered_change(app: str, file_path: str, new_content: str, root=None, max_lines: int = 60) -> str:
    """Render the chart before and after the change; return the changed Kubernetes objects.

    Raises ValueError when the proposal renders with an error or changes nothing.
    """
    root = root or GITOPS_DIR
    chart_dir, namespace = CHARTS[app]
    with tempfile.TemporaryDirectory() as tmp:
        chart = Path(tmp) / app
        shutil.copytree(root / chart_dir, chart)
        before = _objects(_helm_template(chart, app, namespace))
        (chart / Path(file_path).relative_to(chart_dir)).write_text(new_content)
        try:
            after = _objects(_helm_template(chart, app, namespace))
        except ValueError as e:
            raise ValueError(f"the proposed chart does not render: {e}") from None
    lines = []
    for key in sorted(before.keys() | after.keys()):
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        lines += [f"~ {key}" if old and new else (f"+ {key} (new object)" if new else f"- {key} (removed)")]
        lines += list(difflib.unified_diff(yaml.safe_dump(old or {}, sort_keys=True).splitlines(),
                                           yaml.safe_dump(new or {}, sort_keys=True).splitlines(),
                                           lineterm="", n=1))[2:]
    if not lines:
        raise ValueError("the change has NO EFFECT on the rendered Kubernetes objects. In the "
                         "charts/infra/* charts every value must be nested under the chart name "
                         "(e.g. `loki:` -> `singleBinary:` -> ...); check the key path")
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... ({len(lines) - max_lines} more lines)"]
    return "\n".join(lines)


def check_fix(app: str, file_path: str, new_content: str, root=None) -> str:
    """All checks a proposal must pass. Returns the rendered change; raises ValueError."""
    if err := validate_fix(app, file_path, new_content, root=root):
        raise ValueError(err)
    return rendered_change(app, file_path, new_content, root=root)


def describe_fix(tool_call, state, runtime) -> str:
    """HITL description: what the human approves is the diff, not the model's summary."""
    a = tool_call["args"]
    try:
        rendered = check_fix(a.get("app", ""), a.get("file_path", ""), a.get("new_content", ""))
    except ValueError as e:
        rendered = f"VALIDATION FAILED (the tool will refuse this): {e}"
    return (f"Open a PR on {os.environ.get('GITOPS_REPO', 'sre-gitops')}: {a.get('title', '')}\n\n"
            f"Values change:\n```diff\n{fix_diff(a.get('file_path', ''), a.get('new_content', ''))}\n```\n\n"
            f"Rendered Kubernetes change (helm template):\n```diff\n{rendered}\n```")


@tool("validate_fix")
async def validate_fix_tool(app: App, file_path: str, new_content: str) -> str:
    """Check a proposed chart change BEFORE proposing it: path allow-list, YAML syntax, then
    `helm template` of the chart with and without the change. Returns the changed Kubernetes
    objects, or why the change is invalid (render error, or NO EFFECT when the key path is
    wrong). Same arguments as propose_fix_pr. Fix the draft and call again until it passes."""
    try:
        return "VALID. Rendered change:\n" + await asyncio.to_thread(check_fix, app, file_path, new_content)
    except ValueError as e:
        return f"INVALID: {e}"


async def already_handled(gh: httpx.AsyncClient, file_path: str, new_content: str) -> str | None:
    """Why opening this PR would be a duplicate, or None.

    One fault often raises several alerts (e.g. ImagePullBackOff, then PodPending), each its own
    incident; parallel runs can propose the same fix, and a stale approval card can be clicked
    after another PR already fixed it.
    """
    cur = (await gh.get(f"/contents/{file_path}", params={"ref": "main"})).raise_for_status().json()
    if base64.b64decode(cur["content"]).decode() == new_content:
        return f"main already has this exact {file_path}: the fault was fixed by another change"
    # ponytail: first 30 open PRs only; plenty for one repo with human-merged fix PRs
    for pr in (await gh.get("/pulls", params={"state": "open", "base": "main"})).raise_for_status().json():
        if pr["head"]["ref"].startswith("sre-agent/"):
            files = (await gh.get(f"/pulls/{pr['number']}/files")).raise_for_status().json()
            if any(f["filename"] == file_path for f in files):
                return f"PR #{pr['number']} already open for {file_path}: {pr['html_url']}"
    return None


async def add_reviewers(gh: httpx.AsyncClient, pr: dict) -> str:
    """Assign PR_REVIEWERS and request their review. GitHub refuses a review request to the PR's
    author (the GITHUB_PAT owner), so they are assigned instead; a separate bot identity for the
    agent makes the review request work."""
    people = [u.strip() for u in os.environ.get("PR_REVIEWERS", "").split(",") if u.strip()]
    if not people:
        return ""
    notes = []
    r = await gh.post(f"/issues/{pr['number']}/assignees", json={"assignees": people})
    notes.append("assigned " + ",".join(people) if r.is_success else f"assign failed {r.status_code}")
    reviewers = [u for u in people if u.lower() != pr["user"]["login"].lower()]
    if reviewers:
        r = await gh.post(f"/pulls/{pr['number']}/requested_reviewers", json={"reviewers": reviewers})
        notes.append("review requested " + ",".join(reviewers) if r.is_success else f"review request failed {r.status_code}")
    return " (" + "; ".join(notes) + ")"


@tool
async def propose_fix_pr(app: App, file_path: str, new_content: str, title: str, body: str) -> str:
    """Open a pull request on the GitOps repo that replaces ONE YAML file of one chart.

    app is the chart: pulsar, voyager (charts/apps/<app>/) or grafana, loki, prometheus,
    fluent-bit (charts/infra/<app>/). file_path is relative to the repo root, e.g.
    charts/apps/pulsar/values.yaml or charts/infra/loki/values.yaml.
    new_content is the COMPLETE new file (not a patch): copy the current file and change only
    the lines the fix needs. A human approves this call before it runs. Returns the PR URL.
    """
    try:
        await asyncio.to_thread(check_fix, app, file_path, new_content)
    except ValueError as e:
        return f"ERROR: {e}. No PR was opened."
    thread_id = str(get_config()["configurable"]["thread_id"])
    repo, branch = os.environ["GITOPS_REPO"], f"sre-agent/{app}-{thread_id.replace('-', '')[:8]}"
    body = f"{body}\n\nGenerated by sre-agent, thread {thread_id}."
    owner = repo.split("/")[0]
    async with httpx.AsyncClient(base_url=f"{GITHUB_API}/repos/{repo}", headers=github_headers(), timeout=30) as gh:
        if why := await already_handled(gh, file_path, new_content):
            return f"No PR opened: {why}. Report the existing fix instead of a new one."
        main_sha = (await gh.get("/git/ref/heads/main")).raise_for_status().json()["object"]["sha"]
        r = await gh.post("/git/refs", json={"ref": f"refs/heads/{branch}", "sha": main_sha})
        if r.status_code == 422:  # branch exists: a retry of this thread's proposal (FX-04)
            prs = (await gh.get("/pulls", params={"head": f"{owner}:{branch}", "state": "all"})).raise_for_status().json()
            if prs:
                return f"PR already exists: {prs[0]['html_url']}"
        else:
            r.raise_for_status()
        blob = (await gh.get(f"/contents/{file_path}", params={"ref": branch})).raise_for_status().json()
        (await gh.put(f"/contents/{file_path}", json={
            "message": title, "branch": branch, "sha": blob["sha"],
            "content": base64.b64encode(new_content.encode()).decode(),
        })).raise_for_status()
        pr = (await gh.post("/pulls", json={"title": title, "head": branch, "base": "main", "body": body})).raise_for_status().json()
        who = await add_reviewers(gh, pr)
    return f"PR opened: {pr['html_url']}{who}"


@tool
async def recent_changes(app: App, hours: int = 24) -> str:
    """List commits on the GitOps repo's main branch that touched this chart's directory
    (charts/apps/<app>/ or charts/infra/<app>/) in the last `hours` hours (max 72), newest
    first, with each commit's diff for that path."""
    hours = max(1, min(hours, 72))
    repo, path = os.environ["GITOPS_REPO"], CHART_DIRS[app]
    since = (datetime.now(UTC) - timedelta(hours=hours)).isoformat(timespec="seconds")
    async with httpx.AsyncClient(base_url=f"{GITHUB_API}/repos/{repo}", headers=github_headers(), timeout=30) as gh:
        commits = (await gh.get("/commits", params={"path": path, "since": since, "per_page": 5})).raise_for_status().json()
        out = []
        for c in commits:
            detail = (await gh.get(f"/commits/{c['sha']}")).raise_for_status().json()
            patches = "\n".join(f"--- {f['filename']}\n{f.get('patch', '')[:1500]}"
                                for f in detail["files"] if f["filename"].startswith(path + "/"))
            msg = c["commit"]["message"].splitlines()[0]
            out.append(f"{c['sha'][:7]} {c['commit']['committer']['date']} {msg!r}\n{patches}")
    return "\n\n".join(out) or f"No commits touched {path}/ in the last {hours}h."
