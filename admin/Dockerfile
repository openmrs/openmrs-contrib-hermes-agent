# hermes-admin: on-demand tool for managing agents' keys, grants, and Slack settings.
# Root, to hand each agent's agents-data directory to HERMES_UID/HERMES_GID and write workspaces'
# root-only configuration. Its internal network's only other member is litellm.
FROM python:3.12.15-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704

# ssh-keygen, for agents' and workspaces' SSH keys.
RUN apt-get update \
  && apt-get install -y --no-install-recommends openssh-client \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY hermes_admin /app/hermes_admin
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app
ENTRYPOINT ["python", "-m", "hermes_admin"]
