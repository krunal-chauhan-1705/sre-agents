# Builds langchain-ai/agent-chat-ui for sre-agent.
# NEXT_PUBLIC_* values are inlined at build time: rebuild per environment if the
# public URL changes. LANGGRAPH_API_URL is read at runtime (set by the Helm chart).
FROM node:22-alpine AS build
ARG AGENT_CHAT_UI_REF=main
ARG NEXT_PUBLIC_API_URL=http://localhost:3000/api
ARG NEXT_PUBLIC_ASSISTANT_ID=sre_agent
RUN apk add --no-cache git && corepack enable
WORKDIR /app
RUN git clone https://github.com/langchain-ai/agent-chat-ui.git . && git checkout "$AGENT_CHAT_UI_REF"
RUN pnpm install --frozen-lockfile
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL \
    NEXT_PUBLIC_ASSISTANT_ID=$NEXT_PUBLIC_ASSISTANT_ID \
    NEXT_TELEMETRY_DISABLED=1
RUN pnpm build

FROM node:22-alpine
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1
COPY --from=build --chown=node:node /app /app
USER node
EXPOSE 3000
CMD ["node_modules/.bin/next", "start", "-p", "3000"]
