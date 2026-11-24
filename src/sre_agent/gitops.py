"""Read-only checkout of the sre-gitops repo at /tmp/gitops, served to the agent as /gitops/.

Refreshed from the GitHub tarball API (no git binary in the image) at most once a minute,
by a before_agent hook, so every run diagnoses against what ArgoCD is syncing.
"""
import asyncio
import io
import logging
import os
import shutil
import tarfile
import time
from pathlib import Path

import httpx
from langchain.agents.middleware import before_agent

log = logging.getLogger(__name__)

GITOPS_DIR = Path(os.environ.get("GITOPS_DIR", "/tmp/gitops"))
MAX_AGE_S = 60
GITHUB_API = "https://api.github.com"

_lock = asyncio.Lock()
_fetched_at = 0.0


def github_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {os.environ['GITHUB_PAT']}",  # header, never in the URL
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _extract(tar_bytes: bytes) -> None:
    """Unpack the tarball next to GITOPS_DIR, then swap it in by rename."""
    new, old = GITOPS_DIR.with_name(GITOPS_DIR.name + ".new"), GITOPS_DIR.with_name(GITOPS_DIR.name + ".old")
    shutil.rmtree(new, ignore_errors=True)
    shutil.rmtree(old, ignore_errors=True)
    staging = new.with_name(new.name + ".tmp")
    shutil.rmtree(staging, ignore_errors=True)
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
        tar.extractall(staging, filter="data")
    (top,) = staging.iterdir()  # GitHub wraps the tree in "<owner>-<repo>-<sha>/"
    top.rename(new)
    shutil.rmtree(staging)
    # ponytail: two renames leave a microsecond gap with no /tmp/gitops; a read in that gap
    # gets "not found" and the agent retries. FilesystemBackend pins its root path at build
    # time, so a symlink swap wouldn't be seen.
    if GITOPS_DIR.exists():
        GITOPS_DIR.rename(old)
    new.rename(GITOPS_DIR)
    shutil.rmtree(old, ignore_errors=True)


async def refresh_checkout() -> None:
    global _fetched_at
    async with _lock:
        if time.monotonic() - _fetched_at < MAX_AGE_S and GITOPS_DIR.exists():
            return
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http:
                r = await http.get(f"{GITHUB_API}/repos/{os.environ['GITOPS_REPO']}/tarball/main",
                                   headers=github_headers())
                r.raise_for_status()
            await asyncio.to_thread(_extract, r.content)
            _fetched_at = time.monotonic()
        except Exception as e:  # noqa: BLE001 - keep the old checkout, never fail the run
            log.warning("gitops refresh failed, keeping previous checkout: %r", e)


@before_agent
async def refresh_gitops(state, runtime) -> None:
    await refresh_checkout()
