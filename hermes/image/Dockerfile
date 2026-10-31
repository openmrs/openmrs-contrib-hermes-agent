# Hermes plus SSH client settings for reaching the agent's workspace (see hermes/config.yaml). The
# agent runs nothing itself; its terminal, file tools, and code execution run in the workspace.

# Set by compose.yaml, pinned by tag and digest.
ARG HERMES_IMAGE

FROM ${HERMES_IMAGE}
COPY ssh_config /etc/ssh/ssh_config.d/workspace.conf
RUN chmod 644 /etc/ssh/ssh_config.d/workspace.conf
# The workspace home, empty here. Hermes checks that a session's directory exists locally before
# running anything there, so terminal.cwd must be a path that exists in both containers.
RUN mkdir -p /work/home
