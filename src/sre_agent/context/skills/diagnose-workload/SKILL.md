---
name: diagnose-workload
description: Step order for diagnosing an alert on a workload in namespace default or o11y (pod state, events, logs, metrics, recent git changes). Use for every alert.
---

# Diagnose a workload

Go in this order and stop once one root cause explains the alert.

1. **Pod state** (Kubernetes MCP): list pods in the alert's namespace (`default` or `o11y`). Note status, restarts,
   `lastState.terminated.reason` (e.g. `OOMKilled`), waiting reason (e.g. `ImagePullBackOff`).
2. **Events** (Kubernetes MCP): events in that namespace for the pod and its owner. Pull errors,
   probe failures and OOM kills show up here first.
3. **Logs** (Grafana MCP, Loki): follow `loki-queries`. Count errors first, then fetch at most
   20 lines of the one container that matters.
4. **Metrics** (Grafana MCP `query_prometheus`, `datasourceUid: "aftds3eabe7eoa"`,
   `queryType: "instant"`, `endTime: "now"`) only if 1-3 are inconclusive. The metric names
   below are known: don't list metric names or labels first. e.g.
   `kube_pod_container_resource_limits{namespace="default",resource="memory"}` or
   `increase(kube_pod_container_status_restarts_total{namespace="default"}[30m])`.
5. **What changed**: `recent_changes(<chart>)` lists the commits to that chart's directory with
   diffs. A commit shortly before the alert's `startsAt` that changes a related value is the
   prime suspect. Read the current values file (paths in `gitops-repo-map`).

## Cross-service rule

voyager returning 503 usually means **pulsar** is failing (voyager calls it on every
journey). When the alert is about voyager, check pulsar's logs and recent changes too before
blaming voyager.

## Typical root causes in this demo

| Symptom | Look at |
|---|---|
| Tracebacks / 500s in pulsar, 503s in voyager | `faultInjection.failEvery` in pulsar values |
| OOMKilled, CrashLoopBackOff | `resources.limits.memory` |
| ImagePullBackOff / ErrImagePull | `image.tag` (only `latest` exists locally) |
| o11y pod OOMKilled / CrashLoopBackOff | that chart's `resources.limits.memory` (key paths in `gitops-repo-map`) |
| o11y pod Pending | requests too high for the node, or a PVC that can't bind |
| Loki/Grafana/Prometheus crash right after a commit | a config value in that chart's values.yaml; read the pod's last log lines |
