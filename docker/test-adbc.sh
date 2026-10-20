#!/usr/bin/env bash
# The test tools live in a disposable derived image, never the published image.
set -euo pipefail
image=${1:?usage: test-adbc.sh IMAGE}
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
context=$(mktemp -d)
trap 'rm -rf "$context"' EXIT
cp "$here/test-adbc.py" "$context/"
cat > "$context/Dockerfile" <<'EOF'
ARG IMAGE
FROM ${IMAGE}
USER root
RUN apt-get update && apt-get install --no-install-recommends -y python3-venv \
 && python3 -m venv /test-env \
 && /test-env/bin/pip install --no-cache-dir 'adbc-driver-manager==1.12.0' 'pyarrow==26.0.0'
COPY test-adbc.py /test-adbc.py
USER hardcharger
ENTRYPOINT ["/bin/sh", "-ec"]
CMD ["hardcharger-server >/tmp/server.log 2>&1 & server=$!; trap 'kill $server; cat /tmp/server.log' EXIT; /test-env/bin/python /test-adbc.py"]
EOF
docker build --build-arg "IMAGE=$image" -t hc-adbc-test "$context"
docker run --rm -e HARDCHARGER_ADMIN_PASSWORD=smoke hc-adbc-test
test "$(docker image inspect "$image" --format '{{.Config.User}}')" = hardcharger
