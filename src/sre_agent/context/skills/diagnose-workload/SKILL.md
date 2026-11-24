---
name: diagnose-workload
description: Step order for diagnosing an alert on a workload in namespace default (pod state, events, logs, metrics, ArgoCD, recent git changes). Use for every alert.
---

# Diagnose a workload

Go in this order and stop once one root cause explains the alert.

1. **Pod state** (Kubernetes MCP): list pods in `default` for the app. Note status, restarts,
   `lastState.terminated.reason` (e.g. `OOMKilled`), waiting reason (e.g. `ImagePullBackOff`).
2. **Events** (Kubernetes MCP): events in `default` for that pod/deployment. Pull errors,
   probe failures and OOM kills show up here first.
3. **Logs** (Grafana MCP, Loki): follow `loki-queries`. Count errors first, then fetch at most
   20 lines of the one container that matters.
4. **Metrics** (Grafana MCP, Prometheus) only if 1-3 are inconclusive, e.g.
   `kube_pod_container_resource_limits{namespace="default",resource="memory"}` or
   `increase(kube_pod_container_status_restarts_total{namespace="default"}[30m])`.
5. **What changed**: `recent_changes(app)` lists the commits to `charts/apps/<app>/` with diffs.
   A commit shortly before the alert's `startsAt` that changes a related value is the prime
   suspect. Read the current values file under `/gitops/charts/apps/<app>/values.yaml`.

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
