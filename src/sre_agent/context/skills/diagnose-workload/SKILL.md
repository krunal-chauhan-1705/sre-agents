---
name: diagnose-workload
description: Step order and ready-made queries for diagnosing an alert on a workload in namespace default or o11y (workload state from kube-state-metrics, Kubernetes events and logs from Loki, recent git changes). Use for every alert.
---

# Diagnose a workload

Everything goes through the Grafana MCP. Go in this order and stop once one root cause explains
the alert. Prometheus: `query_prometheus`, `datasourceUid: "aftds3eabe7eoa"`, `queryType:
"instant"`, `endTime: "now"`. Loki: `query_loki_logs`, `datasourceUid: "afsy81ikodqm8d"`.

## 1. Workload state (Prometheus, kube-state-metrics)

Always wrap in `max by (...)`: raw series carry ~15 noise labels. Don't list metric names or
labels first; these are known. Replace the namespace.

| Question | PromQL |
|---|---|
| Who is stuck or crashed, and why | `max by (pod,container,reason) (kube_pod_container_status_waiting_reason{namespace="o11y"} == 1) or max by (pod,container,reason) (kube_pod_container_status_last_terminated_reason{namespace="o11y"} == 1)` |
| Restarts | `max by (pod,container) (increase(kube_pod_container_status_restarts_total{namespace="o11y"}[30m])) > 0` |
| Running image (wrong tag?) | `max by (pod,container,image_spec) (kube_pod_container_info{namespace="o11y"})` |
| Memory limit | `max by (pod,container) (kube_pod_container_resource_limits{namespace="o11y",resource="memory"})` |
| Pod phase | `max by (pod,phase) (kube_pod_status_phase{namespace="o11y"} == 1)` |
| Not ready | `max by (pod,container) (kube_pod_container_status_ready{namespace="o11y"} == 0)` |
| Memory used vs limit (cAdvisor is per **pod** here: select `pod=~"pulsar-.*"`, no `container` label) | `max by (pod) (container_memory_working_set_bytes{namespace="default",pod=~"pulsar-.*"}) / max by (pod) (kube_pod_container_resource_limits{namespace="default",resource="memory",container="pulsar"})` |
| CPU cores used (no CPU limits are set; request is 0.01) | `max by (pod) (rate(container_cpu_usage_seconds_total{namespace="default",pod=~"pulsar-.*"}[5m]))` |
| Replicas short | `max by (deployment) (kube_deployment_spec_replicas{namespace="o11y"} - kube_deployment_status_replicas_available{namespace="o11y"}) > 0`; DaemonSets: `max by (daemonset) (kube_daemonset_status_number_unavailable{namespace="o11y"}) > 0` |

The first query is usually enough: `ImagePullBackOff`, `CreateContainerConfigError`,
`CrashLoopBackOff`, `OOMKilled` name the failure class. Waiting series only exist while a
container is waiting, so an empty result means none is.

## 2. Events (Loki, `job="k8s-events"`)

Events carry the exact error text: `secret "x" not found`, `manifest unknown`, probe failures,
failed scheduling, OOM kills. Warnings for the namespace, newest first, at most 20:

`{job="k8s-events", involvedObject_namespace="o11y", type="Warning"} | json | line_format "{{.lastTimestamp}} {{.involvedObject_kind}}/{{.involvedObject_name}} {{.reason}} x{{.count}}: {{.message}}"`

Narrow with `|= "<pod or deployment name>"`. ArgoCD's sync events are in namespace `argocd`
(`involvedObject_kind="Application"`): they show when a commit reached the cluster.
If fluent-bit or Loki is the broken component, there are no new events: skip to step 4.

## 3. Logs (Loki, `job="fluentbit"`)

Follow `loki-queries`: count errors first, then at most 20 lines of the one container that
matters. Skip for config/image failures where the container never started (no logs exist).

## 4. What changed

`recent_changes(<chart>)` lists the commits to that chart's directory with diffs. A commit
shortly before the alert's `startsAt` that changes a related value is the prime suspect. The
diff also gives the previous, working value (e.g. the old Secret name or image tag). Read the
current values file (paths in `gitops-repo-map`).

## Cross-service rule

voyager returning 503 usually means **pulsar** is failing (voyager calls it on every
journey). When the alert is about voyager, check pulsar's logs and recent changes too before
blaming voyager.

## Typical root causes in this demo

| Symptom | Look at |
|---|---|
| Tracebacks / 500s in pulsar, 503s in voyager | `faultInjection.failEvery` in pulsar values |
| OOMKilled, CrashLoopBackOff | `resources.limits.memory` |
| `AppMemoryHigh` (>85% of limit, not yet OOMKilled) | a leak or a too-tight `resources.limits.memory`; check the trend over the last hours before raising the limit |
| `AppCPUHigh` (>0.2 cores for 10m, normal ~0.005) | a traffic spike (loadgen) or a hot loop; logs around the start; no CPU limit to fix in values |
| ImagePullBackOff / ErrImagePull | `image.tag` vs `image_spec` (only `latest` exists for the apps) |
| CreateContainerConfigError | a Secret/ConfigMap name in values; the event says which one is missing |
| o11y pod OOMKilled / CrashLoopBackOff | that chart's `resources.limits.memory` (key paths in `gitops-repo-map`) |
| o11y pod Pending | requests too high for the node, or a PVC that can't bind; the event says which |
| Loki/Grafana/Prometheus crash right after a commit | a config value in that chart's values.yaml; its last log lines |
