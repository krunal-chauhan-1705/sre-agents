---
name: loki-queries
description: How to query this cluster's Loki correctly and cheaply through the Grafana MCP (tools, labels, the ANSI status-code trap, double-encoded lines, health-check noise, what to do when Grafana/Loki is down). Use before any Loki query.
---

# Loki queries on this cluster

Measured against the live stack. Ignoring these gives empty results that look like "healthy",
or floods of tokens.

## Tools (Grafana MCP, Loki datasource `afsy81ikodqm8d`)

Loki is reached through Grafana; there is no separate Loki server. You have three Loki tools:

| Tool | Use it for |
|---|---|
| `query_loki_logs` | Everything: counts (metric LogQL, `queryType: "instant"`) and lines (`"range"`) |
| `list_loki_label_values` | Only to check whether a container logged at all (`labelName: "kubernetes_container_name"`, 24h window) |
| `list_loki_label_names` | Not needed: the labels are listed below |

Call shape for `query_loki_logs`: `datasourceUid: "afsy81ikodqm8d"`, `logql`, `startRfc3339`,
`endRfc3339` (relative values like `"now-30m"` and `"now"` work), `limit` (always set; at most
20 for lines), `queryType`. The default window is the last hour and the default limit is 10.

## Quoting

LogQL strings take **double quotes or backticks only**. Single quotes are a parse error
(`invalid char literal`): write `{kubernetes_container_name="pulsar"}`, never `'pulsar'`.

## Labels

- Select with `kubernetes_namespace_name` and `kubernetes_container_name`, e.g.
  `{kubernetes_namespace_name="default", kubernetes_container_name="pulsar"}`.
  Containers: `default` has pulsar, voyager, loadgen; `o11y` has grafana, loki,
  prometheus-server, alertmanager, fluent-bit, kube-state-metrics.
- `job` and `service_name` are always `fluentbit`: never select on them.
- `kubernetes_pod_name` changes on every restart; avoid it.
- If a selector returns nothing, check `list_loki_label_values` over the last 24h before
  concluding the service has no logs. Absence of logs is not absence of a service.

## Status codes: the ANSI trap

Flask colours non-2xx access lines; the colour reset sits right before the closing quote.
Match status codes as quote-space-code-space: `|~ "\" 5[0-9][0-9] "`. A pattern anchored on
`HTTP/1.1" 5..` misses every error.

## Count first, then lines

1. Count (one call, about 50 tokens however many lines match), with `queryType: "instant"`:
   `sum by (kubernetes_container_name) (count_over_time({kubernetes_namespace_name="default"} |~ "Traceback|\" 5[0-9][0-9] " [15m]))`
   For o11y alerts use `kubernetes_namespace_name="o11y"` and match `level=error|panic|Traceback`.
2. Then at most 20 lines of ONE container, without health checks, extracting only the app
   message: `{kubernetes_namespace_name="default", kubernetes_container_name="pulsar"} !~ "/health" |~ "Traceback|Error|\" 5[0-9][0-9] " | json log="log" | line_format "{{.log}}"`
   Use `| json log="log"`, not bare `| json` (which returns the whole kubernetes blob).

## Windows

Default to the last 15-30 minutes around the alert's `startsAt`. Always pass a limit.

## When Grafana or Loki is the broken component

Every Loki tool fails or returns nothing when Grafana, Loki or fluent-bit is down. That is
not "no errors". Use Kubernetes MCP `pods_log` (`namespace`, `name`, `tail: 50`, and
`previous: true` for a container that just crashed) and say in the evidence that Loki was
unavailable.

## What is not in the logs

Response bodies (voyager's `{"error":"pulsar unreachable"}` is never logged: detect it as
status 503). No trace ids: correlate services by timestamp + status.
