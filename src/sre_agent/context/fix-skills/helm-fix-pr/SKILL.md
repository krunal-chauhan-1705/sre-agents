---
name: helm-fix-pr
description: How to write the minimal Helm values change and the PR for it with propose_fix_pr, for app charts and o11y charts. Use for every fix proposal.
---

# Propose a Helm values fix

1. Read the current file: `/gitops/charts/apps/<chart>/values.yaml` for pulsar/voyager, or
   `/gitops/charts/infra/<chart>/values.yaml` for grafana, loki, prometheus, fluent-bit.
   In the infra charts every value is nested under the chart name (e.g. `loki:` then
   `singleBinary:`): keep that nesting, or Helm ignores the change.
2. Build `new_content` = the current file with ONLY the keys the fix needs changed. Keep every
   other line, comment and the key order identical. Restore the known-good value (see the
   diagnosis) rather than inventing a new one.
3. In ONE turn, write the draft to `/drafts/<chart>-<file name>` (scratch, visible to the human
   in Chat UI) and call `validate_fix` with the same arguments you will give `propose_fix_pr`.
   - `VALID`: check the rendered change is the one you intended (right object, right field).
   - `DUPLICATE`: the same file already has an open PR, is already fixed on main, or has a fix
     waiting for approval in another incident (one fault often raises several alerts). Stop:
     do not call `propose_fix_pr`; report the existing fix.
   - `INVALID ... NO EFFECT`: the key path is wrong (usually missing the chart-name nesting in
     `charts/infra/*`). Fix it and validate again.
   - `INVALID ... does not render`: fix the YAML/values and validate again.
   Stop after 3 failed validations and report why instead of proposing.
4. Call `propose_fix_pr` once with exactly the validated arguments:
   - `app`: the chart: `pulsar`, `voyager`, `grafana`, `loki`, `prometheus` or `fluent-bit`
   - `file_path`: e.g. `charts/apps/pulsar/values.yaml` or `charts/infra/loki/values.yaml`
     (relative to the repo root, no `/gitops/` prefix)
   - `new_content`: the complete file
   - `title`: `fix(<chart>): <change>`, e.g. `fix(loki): restore memory limit (128Mi -> 768Mi)`
   - `body`: the template below (the tool appends the sre-agent thread id)
5. The human sees the values diff and the rendered Kubernetes change, then may approve,
   edit or reject. If rejected, report that no PR was opened, with the
   reviewer's reason. Never retry a rejected proposal.
6. Return the PR URL exactly as the tool returned it, or the reason there is none.

## PR body template

```
## Alert
<alertname> in namespace <default|o11y>, since <startsAt>

## Root cause
<one or two sentences>

## Evidence
- <source: tool + query/resource> - <finding>
- <suspect commit sha + message, if any>

## Change
`<full.key.path>: <old> -> <new>` in `<file_path>`. Minimal: no other keys touched.

## Rendered change (helm template)
<the object and field from validate_fix, e.g. `StatefulSet/loki` memory limit 64Mi -> 768Mi>

## Rollback
Revert this PR; ArgoCD re-syncs the previous value.
