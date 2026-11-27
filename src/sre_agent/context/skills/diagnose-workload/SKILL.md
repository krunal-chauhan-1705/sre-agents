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
| Memory used vs limit (cAdvisor is per **pod** here: select by `pod`, no `container` label) | `max by (pod) (container_memory_working_set_bytes{namespace="default",pod=~"pulsar-.*"}) / max by (pod) (kube_pod_container_resource_limits{namespace="default",resource="memory",container="pulsar"})` |
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

## Cluster, node and event alerts

| Alert | First query |
|---|---|
| `NodeMemoryHigh` / `NodeCPUHigh` | top consumers: `topk(5, max by (namespace,pod) (container_memory_working_set_bytes{pod!=""}))` or `topk(5, max by (namespace,pod) (rate(container_cpu_usage_seconds_total{pod!=""}[5m])))` |
| `NodeDiskSpaceLow` / `NodeDiskFillingIn6h` | `min by (mountpoint) (node_filesystem_avail_bytes{fstype="ext4"} / node_filesystem_size_bytes{fstype="ext4"})`. On minikube the usual cause is old container images (`docker image prune` / `minikube image rm`): a human action, never a PR |
| `NodeNotReady`, `NodePressure`, `NodeUnschedulable` | `max by (condition,status) (kube_node_status_condition == 1)`, then node events: `{job="k8s-events", involvedObject_kind="Node"}` |
| `KubeWarningEvents` | the alert's `reason` and `namespace`: list those events (step 2) with `reason="<reason>"` |
| `PodProbeFailures` / `PodNotReady` | probe events in the namespace (`reason="Unhealthy"`): which pod, liveness or readiness, timeout or HTTP error |
| `ArgoCDSyncFailing` (a commit never reached the cluster: the live app still runs the old version) | the sync error from the ArgoCD controller logs: `{kubernetes_namespace_name="argocd", kubernetes_container_name="application-controller"} \|= "application=<app>" \|~ "Sync operation to .* failed" \| json log="log" \| line_format "{{.log}}"`. The message is the Kubernetes API's rejection (e.g. `requests ... must be less than or equal to memory limit`, `field is immutable`, a bad image or schema). Then `recent_changes(<app>)`: the commit named in the message is the cause; the fix restores a valid value in that chart. ArgoCD application name = chart name |
| `ArgoCDAppProblem` | `{job="k8s-events", involvedObject_kind="Application"} \|= "<app>"`: the last health/sync messages, then `recent_changes` for that chart |
| `TargetDown`, `KubeStateMetricsMissing`, `PrometheusRuleFailures` | the monitoring pipeline is the broken component: say which alerts are blind until it is back |
| `PersistentVolumeClaimPending` / `PersistentVolumeFailed` | events for the PVC (`involvedObject_kind="PersistentVolumeClaim"`): storage class, provisioning errors |
| `StatefulSetReplicasMismatch`, `DaemonSetUnavailable`, `DeploymentRolloutStuck`, `PodFailedOrEvicted`, `JobFailed` | step 1 state queries for that namespace, then step 2 events |

## Cross-service rule

voyager returning 503 usually means **pulsar** is failing (voyager calls it on every
journey). When the alert is about voyager, check pulsar's logs and recent changes too before
blaming voyager.

## Typical root causes in this demo

| Symptom | Look at |
|---|---|
| Tracebacks / 500s in pulsar, 503s in voyager | `faultInjection.failEvery` in pulsar values |
| OOMKilled, CrashLoopBackOff | `resources.limits.memory` |
| `PodMemoryNearLimit` (any pod >90% of its memory limit for 5m) | a leak or a too-tight `resources.limits.memory`; check the trend (`max_over_time(...[6h])`) before raising the limit. For a fixable chart, raising the limit is a valid PR |
| `PodCPUHigh` (any pod >0.75 cores for 15m, info) | a traffic spike or a hot loop: logs around the start. Only the agent's own pods have CPU limits, so there is usually no value to fix |
| ImagePullBackOff / ErrImagePull | `image.tag` vs `image_spec` (only `latest` exists for the apps) |
| CreateContainerConfigError | a Secret/ConfigMap name in values; the event says which one is missing |
| o11y pod OOMKilled / CrashLoopBackOff | that chart's `resources.limits.memory` (key paths in `gitops-repo-map`) |
| o11y pod Pending | requests too high for the node, or a PVC that can't bind; the event says which |
| Loki/Grafana/Prometheus crash right after a commit | a config value in that chart's values.yaml; its last log lines |
