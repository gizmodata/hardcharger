# The Hardcharger server image. Built by .github/workflows/server.yml from the release
# tarballs (one per architecture), so the image runs exactly the tested binary.
FROM ubuntu:24.04

ARG TARGETARCH
RUN apt-get update \
 && apt-get install --no-install-recommends -y ca-certificates libssl3t64 tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --create-home --home-dir /var/lib/hardcharger hardcharger

COPY dist/hardcharger-linux-${TARGETARCH}/ /opt/hardcharger/
RUN ln -s /opt/hardcharger/bin/hardcharger-server /usr/local/bin/hardcharger-server

USER hardcharger
WORKDIR /var/lib/hardcharger
ENV HARDCHARGER_HOST=0.0.0.0 \
    HARDCHARGER_PORT=31337 \
    HARDCHARGER_HEALTH_PORT=31338
EXPOSE 31337 31338
ENTRYPOINT ["hardcharger-server"]
