---
name: gitops-repo-map
description: Map a workload or alert to its ArgoCD Application, Helm chart and values file in the sre-gitops repo (/gitops/). Use when you need to know where a setting lives.
---

# sre-gitops layout (read-only under /gitops/)

| Workload (ns) | ArgoCD Application | Chart dir | Values file |
|---|---|---|---|
| pulsar (default) | `apps/pulsar.yaml` | `charts/apps/pulsar/` | `charts/apps/pulsar/values.yaml` |
| voyager (default) | `apps/voyager.yaml` | `charts/apps/voyager/` | `charts/apps/voyager/values.yaml` |
| loadgen (default) | `apps/loadgen.yaml` | `charts/apps/loadgen/` | not fixable by the agent |
| grafana, loki, prometheus, fluent-bit, mcp-* (o11y) | `apps/<name>.yaml` | `charts/infra/<name>/` | not fixable by the agent |

## Values that matter (pulsar and voyager)

| Key | Rendered as | Known-good value |
|---|---|---|
| `image.tag` | container image `pulsar:<tag>` / `voyager:<tag>` (local images only) | `latest` |
| `resources.requests.memory` / `resources.limits.memory` | container resources | `64Mi` / `128Mi` |
| `faultInjection.failEvery` (pulsar) | env `PULSE_FAIL_EVERY`: every Nth `/pulsar/pulse` call raises a 500 | `0` (off) |
| `pulsar.serviceUrl` (voyager) | env `PULSAR_URL` | `http://pulsar:5001` |
| `replicaCount` | Deployment replicas | `1` |

Templates are in `charts/apps/<app>/templates/`; read them if you need to confirm how a value
is rendered. Only files under `charts/apps/pulsar/` and `charts/apps/voyager/` can be changed,
and only through `fix-agent`.
