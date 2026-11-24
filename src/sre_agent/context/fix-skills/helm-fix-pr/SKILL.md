---
name: helm-fix-pr
description: How to write the minimal Helm values change and the PR for it with propose_fix_pr. Use for every fix proposal.
---

# Propose a Helm values fix

1. Read the current file: `/gitops/charts/apps/<app>/values.yaml`.
2. Build `new_content` = the current file with ONLY the keys the fix needs changed. Keep every
   other line, comment and the key order identical. Restore the known-good value (see the
   diagnosis) rather than inventing a new one.
3. Call `propose_fix_pr` once with:
   - `app`: `pulsar` or `voyager`
   - `file_path`: `charts/apps/<app>/values.yaml` (relative to the repo root, no `/gitops/`)
   - `new_content`: the complete file
   - `title`: `fix(<app>): <change>`, e.g. `fix(pulsar): disable fault injection (failEvery 3 -> 0)`
   - `body`: the template below (the tool appends the sre-agent thread id)
4. The human may approve, edit or reject. If rejected, report that no PR was opened, with the
   reviewer's reason. Never retry a rejected proposal.
5. Return the PR URL exactly as the tool returned it, or the reason there is none.

## PR body template

```
## Alert
<alertname> in namespace default, since <startsAt>

## Root cause
<one or two sentences>

## Evidence
- <source: tool + query/resource> - <finding>
- <suspect commit sha + message, if any>

## Change
`<key>: <old> -> <new>` in `charts/apps/<app>/values.yaml`. Minimal: no other keys touched.

## Rollback
Revert this PR; ArgoCD re-syncs the previous value.
