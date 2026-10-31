# Mints short-lived GitHub App installation tokens for agents. Standard library only; JWTs are
# signed with the image's openssl CLI.
FROM python:3.12.15-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704

COPY server.py /app/server.py
USER nobody
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "/app/server.py"]
