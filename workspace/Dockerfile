# workspace: where an agent's commands run, over SSH (Hermes' `ssh` terminal backend), away from the
# agent's tokens. Has JDKs 8, 11, 17, and 21, Maven, Node with Yarn and pnpm (via Corepack), and gh.
#
# Images are pinned by tag and digest. To update: a JDK or Node, its FROM line; Maven, MAVEN_VERSION
# and MAVEN_SHA512 (from apache-maven-<version>-bin.tar.gz.sha512); gh, GH_VERSION and both checksums
# (from gh_<version>_checksums.txt on the release page).

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

# The slim image has no tar or sha256sum, hence Python.
FROM python:3.12.15-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704 AS gh
ARG GH_VERSION=2.103.3
ARG GH_SHA256_AMD64=4c806978ffce1cec70853302a56d70ac6da4f19bae7adca4e1a8ab00633dd91d
ARG GH_SHA256_ARM64=ee32df2323c8847f5fac6cb6b4b69ca75828e9329a8f22f677021f7b3306ce63
COPY fetch_gh.py /fetch_gh.py
RUN python3 /fetch_gh.py

FROM python:3.12.15-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704
RUN apt-get update \
  && apt-get install -y --no-install-recommends git ca-certificates curl openssh-server \
  && rm -rf /var/lib/apt/lists/* /etc/ssh/ssh_host_*
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

COPY --from=gh /gh /usr/local/lib/gh/gh
COPY gh /usr/local/bin/gh
COPY gitconfig /etc/gitconfig
RUN chmod 755 /usr/local/bin/gh && chmod 644 /etc/gitconfig

# The agent's login user. Password "*", not useradd's "!", which sshd treats as locked even for keys.
ARG WORKSPACE_UID=20000
RUN groupadd -g "$WORKSPACE_UID" workspace \
  && useradd -u "$WORKSPACE_UID" -g workspace -d /work/home -M -s /bin/bash workspace \
  && usermod -p '*' workspace

COPY supervisor.py /app/supervisor.py
ENV PYTHONUNBUFFERED=1
# Root, to run sshd and kill and clean up after the workspace user.
ENTRYPOINT ["python", "/app/supervisor.py"]
