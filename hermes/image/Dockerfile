# The Hermes image plus the GitHub CLI. `gh` is wrapped so every run gets a short-lived token from
# the github-token service, and git uses it as its GitHub credential helper; no GitHub credential is
# stored in the agent.
#
# To update gh, change GH_VERSION and both checksums (from gh_<version>_checksums.txt on the
# release page), then rebuild: docker compose build.

# Set by compose.yaml: the Hermes image, pinned by tag and digest.
ARG HERMES_IMAGE

# The Hermes image has no tar or sha256sum, so gh is fetched and verified here and only the binary
# is copied across.
FROM python:3.12.15-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704 AS gh
ARG GH_VERSION=2.103.3
ARG GH_SHA256_AMD64=4c806978ffce1cec70853302a56d70ac6da4f19bae7adca4e1a8ab00633dd91d
ARG GH_SHA256_ARM64=ee32df2323c8847f5fac6cb6b4b69ca75828e9329a8f22f677021f7b3306ce63
COPY fetch_gh.py /fetch_gh.py
RUN python3 /fetch_gh.py

FROM ${HERMES_IMAGE}
COPY --from=gh /gh /usr/local/lib/gh/gh
COPY gh /usr/local/bin/gh
COPY gitconfig /etc/gitconfig
RUN chmod 755 /usr/local/bin/gh && chmod 644 /etc/gitconfig
