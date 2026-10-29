# sandbox: builds and tests openmrs repositories for agents, away from their tokens and data.
# Has JDKs 8, 11, 17, and 21, Maven, and Node with Yarn and pnpm (through Corepack).
#
# Every image is pinned by tag and digest. To update a JDK or Node, change its FROM line; to update
# Maven, change MAVEN_VERSION and MAVEN_SHA512 (from apache-maven-<version>-bin.tar.gz.sha512).

FROM eclipse-temurin:8u502-b07-jdk-noble@sha256:c220651a87a3da8287e7cb9c85252747f27b8360149e0f70f6277aa597ae5bac AS jdk8
FROM eclipse-temurin:11.0.32_9-jdk-noble@sha256:ffd3f758c43f9850b6975ebfd18c4407a6b4605d590294132ffc8ed8d1930ee6 AS jdk11
FROM eclipse-temurin:17.0.20_8-jdk-noble@sha256:c2c0deda43a14dea9d5c6c5349d22538c385c7d678d03dc6a133fe0656ed0d64 AS jdk17
FROM eclipse-temurin:21.0.12_8-jdk-noble@sha256:4d271cd5e0624598cf563342f47281b09cb364bc13acbbd7251f49f83470018d AS jdk21
FROM node:22.23.2-trixie-slim@sha256:c5849ff9c9ebcd66615412f0b548ca5b8ecaef84003dc9ac2e077ebe46aaa3f6 AS node

FROM python:3.12.15-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704 AS maven
ARG MAVEN_VERSION=3.9.16
ARG MAVEN_SHA512=831a8591fe20c8243b1dbe7d71e3244f31d1665b0804b2e825e38cbbe5ce0cafb8338851f90780735568773e0a6cd07bbec107cda0b896b008b861075358b6f6
COPY fetch_maven.py /fetch_maven.py
RUN python3 /fetch_maven.py

FROM python:3.12.15-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704
RUN apt-get update \
  && apt-get install -y --no-install-recommends git ca-certificates \
  && rm -rf /var/lib/apt/lists/*
COPY --from=jdk8 /opt/java/openjdk /opt/java/8
COPY --from=jdk11 /opt/java/openjdk /opt/java/11
COPY --from=jdk17 /opt/java/openjdk /opt/java/17
COPY --from=jdk21 /opt/java/openjdk /opt/java/21
COPY --from=maven /maven /opt/maven
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
  && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
  && ln -s ../lib/node_modules/corepack/dist/corepack.js /usr/local/bin/corepack \
  && corepack enable --install-directory /usr/local/bin yarn pnpm \
  && ln -s /opt/maven/bin/mvn /usr/local/bin/mvn

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt
COPY server.py /app/server.py
ENV PYTHONUNBUFFERED=1
# Starts as root to run each build as the unprivileged build user and clean up after it.
ENTRYPOINT ["python", "/app/server.py"]
