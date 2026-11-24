---
name: loki-queries
description: How to query this cluster's Loki correctly and cheaply (labels, the ANSI status-code trap, double-encoded lines, health-check noise). Use before any Loki query.
---

# Loki queries on this cluster

Measured against the live stack (ai-o11y knowledge hub). Ignoring these gives empty results
that look like "healthy", or floods of tokens.

## Labels

- Select with `kubernetes_namespace_name` and `kubernetes_container_name`, e.g.
  `{kubernetes_namespace_name="default", kubernetes_container_name="pulsar"}`.
- `job` and `service_name` are always `fluentbit`: never select on them.
- `kubernetes_pod_name` changes on every restart; avoid it.
- If a selector returns nothing, list label values over the last 24h before concluding the
  service has no logs. Absence of logs is not absence of a service.

## Status codes: the ANSI trap

Flask colours non-2xx access lines; the colour reset sits right before the closing quote.
Match status codes as quote-space-code-space: `|~ "\" 5[0-9][0-9] "`. A pattern anchored on
`HTTP/1.1" 5..` misses every error.

## Count first, then lines

1. Count: `sum by (kubernetes_container_name) (count_over_time({kubernetes_namespace_name="default"} |~ "Traceback|\" 5[0-9][0-9] " [15m]))`
2. Then at most 20 lines of ONE container, without health checks, extracting only the app
   message: `{kubernetes_namespace_name="default", kubernetes_container_name="pulsar"} !~ "/health" |~ "Traceback|Error|\" 5[0-9][0-9] " | json log="log" | line_format "{{.log}}"`
   Use `| json log="log"`, not bare `| json` (which returns the whole kubernetes blob).

## Windows

Default to the last 15-30 minutes around the alert's `startsAt`. Always pass a limit.

## What is not in the logs

Response bodies (voyager's `{"error":"pulsar unreachable"}` is never logged: detect it as
status 503). No trace ids: correlate services by timestamp + status.
