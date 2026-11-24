# sre-agents

Entry-grade SRE agent on a self-hosted LangGraph Agent Server (split API/queue). An
Alertmanager webhook starts a run; the main agent diagnoses with read-only Grafana (Loki,
Prometheus) and Kubernetes MCP servers plus the GitOps repo; the `fix-agent` subagent proposes
a Helm values change as a PR on `sre-gitops`, which a human approves (HITL) and merges.

GitOps repo (charts, ArgoCD apps): https://github.com/krunal-chauhan-1705/sre-gitops

```
langgraph.json                graph sre_agent, custom routes, webhook allow-list, checkpoint TTL
src/sre_agent/agent.py        create_deep_agent: backend routes, permissions, fix-agent subagent + HITL
                              /gitops/ = sre-gitops main, mounted read-only by a git-sync sidecar (chart)
src/sre_agent/tools.py        validate_fix (helm template check), propose_fix_pr (the only write), recent_changes
src/sre_agent/mcp.py          in-cluster Grafana + Kubernetes MCP servers
src/sre_agent/webhook.py      POST /alertmanager, POST /agent-events
src/sre_agent/context/        AGENTS.md, skills/ (main agent), fix-skills/ (fix-agent)
tests/                        no network, fake model
deploy/chat-ui/Dockerfile     Agent Chat UI for assistant sre_agent
```

## Test

```sh
uv venv -p 3.12 .venv && uv pip install -p .venv/bin/python -e . pytest
.venv/bin/python -m pytest -q tests
```

## Build into minikube (no registry; the chart uses pullPolicy IfNotPresent)

`langgraph build` generates the Agent Server image from `langgraph.json` (base image, this
package, the custom routes, and `dockerfile_lines`, which adds the `helm` binary that
`validate_fix` uses for offline rendering). Point the Docker CLI at minikube's daemon so the
image lands straight in the cluster:

```sh
eval $(minikube docker-env)
langgraph build -t sre-agent:<tag>
minikube image build -t sre-agent-chat-ui:0.1.0 -f Dockerfile deploy/chat-ui   # -f is relative to the context
eval $(minikube docker-env -u)
```

Bump the tag in `sre-gitops/charts/infra/sre-agent/values.yaml` for every rebuild: the same tag
is never re-pulled.

## Secret (created by hand, never committed)

```sh
kubectl --context=minikube create ns sre-agent
kubectl --context=minikube -n sre-agent create secret generic sre-agent-secrets \
  --from-literal=api_key=<LangSmith API key> \
  --from-literal=OPENROUTER_API_KEY=<key> \
  --from-literal=GITHUB_PAT=<fine-grained PAT: sre-gitops only, Contents RW + Pull requests RW>
```

## Use

```sh
kubectl --context=minikube -n sre-agent port-forward svc/sre-agent-chat-ui 3000:3000      # Chat UI, approvals + edits
kubectl --context=minikube -n sre-agent port-forward svc/sre-agent-api-server 8123:80     # API / LangSmith Studio
```
