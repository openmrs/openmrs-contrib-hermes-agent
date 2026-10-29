# hermes-admin: on-demand tool for managing agents' LiteLLM keys, MCP and GitHub access, and Slack
# settings.
# Runs as root because it creates each agent's directory in the agents-data volume and hands it to
# the Hermes runtime user (HERMES_UID/HERMES_GID). It only runs on demand, on an internal network
# whose only other member is litellm.
FROM python:3.12.15-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY hermes_admin /app/hermes_admin
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app
ENTRYPOINT ["python", "-m", "hermes_admin"]
