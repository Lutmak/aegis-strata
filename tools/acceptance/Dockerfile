# Sandbox for the acceptance runs (PLAN.md step 13): pi, the coding agent, with the tools a work item needs. The
# model server is outside the container (an ssh tunnel on the host), reached as host.docker.internal.
FROM node:22-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      git python3 python3-pip python3-venv python3-pytest curl ca-certificates jq procps make gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g @earendil-works/pi-coding-agent@0.85.1 && pi --version
# uid 1000 is "node" in the base image; the agent runs as it
USER node
WORKDIR /work
ENV PI_OFFLINE=1
ENTRYPOINT ["pi"]
