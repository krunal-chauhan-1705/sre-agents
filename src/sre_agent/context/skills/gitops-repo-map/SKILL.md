---
name: gitops-repo-map
description: Map a workload or alert to its ArgoCD Application, Helm chart, values file and exact key paths in the sre-gitops repo (/gitops/main/), for apps and the o11y stack. Use when you need to know where a setting lives.
---

# sre-gitops layout (read-only under /gitops/main/)

Paths below are relative to the repo root: prefix them with `/gitops/main/` to read them.

Every chart has one ArgoCD Application `apps/<chart>.yaml` with the same name as its release.

| Workload (ns) | Chart (= `app` for fix-agent) | Values file | Fixable |
|---|---|---|---|
| pulsar (default) | `pulsar` | `charts/apps/pulsar/values.yaml` | yes |
| voyager (default) | `voyager` | `charts/apps/voyager/values.yaml` | yes |
| grafana (o11y) | `grafana` | `charts/infra/grafana/values.yaml` | yes |
| loki-0 + loki-sc-rules sidecar (o11y) | `loki` | `charts/infra/loki/values.yaml`, rule in `charts/infra/loki/templates/app-error-rule.yaml` | yes |
| prometheus-server, prometheus-alertmanager, kube-state-metrics, node-exporter (o11y) | `prometheus` | `charts/infra/prometheus/values.yaml` | yes |
| fluent-bit DaemonSet (o11y) | `fluent-bit` | `charts/infra/fluent-bit/values.yaml` | yes |
| loadgen (default) | `loadgen` | `charts/apps/loadgen/values.yaml` | no |
| mcp-grafana, mcp-kubernetes (o11y), sre-agent (sre-agent) | - | - | **never**: your own tools |

## Values that matter (pulsar and voyager)

| Key | Rendered as | Known-good value |
|---|---|---|
| `image.tag` | container image `pulsar:<tag>` / `voyager:<tag>` (local images only) | `latest` |
| `resources.requests.memory` / `resources.limits.memory` | container resources | `64Mi` / `128Mi` |
| `faultInjection.failEvery` (pulsar) | env `PULSE_FAIL_EVERY`: every Nth `/pulsar/pulse` call raises a 500 | `0` (off) |
| `pulsar.serviceUrl` (voyager) | env `PULSAR_URL` | `http://pulsar:5001` |
| `replicaCount` | Deployment replicas | `1` |

## o11y charts: values are nested under the subchart key

The infra charts wrap an upstream chart, so every value sits under its name. A key without
that prefix is silently ignored by Helm (the fix would merge but change nothing).

| Chart | Key path (known-good) |
|---|---|
| grafana | `grafana.resources.limits.memory` (`256Mi`), `grafana.resources.requests.memory` (`128Mi`), `grafana.persistence.size` (`2Gi`), `grafana.datasources."datasources.yaml"` (Prometheus `aftds3eabe7eoa`, Loki `afsy81ikodqm8d`) |
| loki | `loki.singleBinary.resources.limits.memory` (`768Mi`), `loki.singleBinary.persistence.size` (`4Gi`), `loki.loki.rulerConfig.alertmanager_url` (`http://prometheus-alertmanager.o11y.svc.cluster.local:9093`), `loki.deploymentMode` (`SingleBinary`; read/write/backend replicas `0`) |
| prometheus | `prometheus.server.resources.limits.memory` (`768Mi`), `prometheus.server.retention` (`3d`), `prometheus.alertmanager.resources.limits.memory` (`128Mi`), `prometheus.alertmanager.config` (receivers + route), `prometheus.serverFiles."alerting_rules.yml"`, `prometheus.kube-state-metrics.resources` |
| fluent-bit | `fluent-bit.resources.limits.memory` (`128Mi`), `fluent-bit.config.outputs` (Host `loki.o11y.svc.cluster.local`, Port `3100`) |

Upstream defaults live in the vendored `charts/infra/<chart>/charts/*.tgz`, which you can't
read: if a key isn't in the wrapper's values.yaml, say so instead of guessing its default.
Only change charts through `fix-agent`.
