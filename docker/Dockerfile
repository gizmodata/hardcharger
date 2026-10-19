# The Hardcharger server image, with the command-line client (`hardcharger`) on PATH too.
# Built by .github/workflows/server.yml from the release tarballs (one per architecture),
# so the image runs exactly the tested binaries. They are built for glibc 2.28 with
# every dependency (DuckDB and its extensions, OpenSSL, Arrow, ...) linked in statically,
# so the image needs no libraries beyond glibc and libstdc++, only the CA certificates
# and time zones.
FROM ubuntu:24.04

ARG TARGETARCH
RUN apt-get update \
 && apt-get install --no-install-recommends -y ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --create-home --home-dir /var/lib/hardcharger hardcharger

# The tarball's tree: bin/, LICENSE, CHANGELOG.md and the third-party notices, which are
# also linked where Debian-style images keep each package's (/usr/share/doc/<package>/).
COPY dist/hardcharger-linux-${TARGETARCH}/ /opt/hardcharger/
RUN ln -s /opt/hardcharger/bin/hardcharger-server /usr/local/bin/hardcharger-server \
 && ln -s /opt/hardcharger/bin/hardcharger /usr/local/bin/hardcharger \
 && mkdir -p /usr/share/doc/hardcharger \
 && for f in LICENSE THIRD_PARTY_NOTICES.md THIRD_PARTY_NOTICES-web-ui.txt; do \
      test -f "/opt/hardcharger/$f" && ln -s "/opt/hardcharger/$f" "/usr/share/doc/hardcharger/$f" || exit 1; \
    done

USER hardcharger
WORKDIR /var/lib/hardcharger
ENV HARDCHARGER_HOST=0.0.0.0 \
    HARDCHARGER_PORT=31337 \
    HARDCHARGER_HEALTH_PORT=31338
EXPOSE 31337 31338
ENTRYPOINT ["hardcharger-server"]
