#!/usr/bin/env bash
#
# Development certificates for the internal TLS of spec 22.1.
#
# One CA signs one server certificate per data store, so the application can
# connect with sslmode=verify-full and a wrong or missing certificate fails
# closed. Everything lands in ./certs, which is gitignored.
#
# These are development certificates. They are regenerated freely, they are
# readable by anyone who can read the working tree, and they must never reach
# production — which brings its own CA.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CERTS="${ROOT}/certs"
DAYS="${FITTRACK_DEV_CERT_DAYS:-825}"

if [ -f "${CERTS}/ca/ca.crt" ] && [ "${1:-}" != "--force" ]; then
  echo "certs/ already exists; pass --force to regenerate."
  exit 0
fi

# Running `docker compose up` before this script leaves an empty root-owned
# `certs/` behind: Docker creates a missing bind-mount source itself, as root.
# The services then start and fail their handshake with an error that points
# nowhere near the cause, so say the cause out loud.
if [ -d "${CERTS}" ] && ! rm -rf "${CERTS}" 2>/dev/null; then
  cat >&2 <<MSG
${CERTS} exists and is not writable by $(id -un).

Docker created it as root when a compose service tried to bind-mount a
certificate directory that did not exist yet. Remove it and run this again:

  docker run --rm -v "${ROOT}:/work" alpine:3.21 rm -rf /work/certs
  make certs

Run \`make up\` rather than \`docker compose up\` to avoid this: it generates the
certificates first.
MSG
  exit 1
fi

mkdir -p "${CERTS}/ca"

echo "==> certificate authority"
openssl req -x509 -newkey rsa:4096 -sha256 -days "${DAYS}" -nodes \
  -keyout "${CERTS}/ca/ca.key" -out "${CERTS}/ca/ca.crt" \
  -subj "/CN=FitTrack Development CA/O=FitTrack/OU=dev" \
  -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null

for service in postgres redis qdrant; do
  echo "==> ${service}"
  mkdir -p "${CERTS}/${service}"
  openssl req -newkey rsa:2048 -sha256 -nodes \
    -keyout "${CERTS}/${service}/server.key" \
    -out "${CERTS}/${service}/server.csr" \
    -subj "/CN=${service}/O=FitTrack/OU=dev" 2>/dev/null

  # The SAN carries both the compose service name and localhost: the same
  # certificate has to verify from inside the network and from the host, where
  # the dev override publishes the port.
  openssl x509 -req -in "${CERTS}/${service}/server.csr" \
    -CA "${CERTS}/ca/ca.crt" -CAkey "${CERTS}/ca/ca.key" -CAcreateserial \
    -out "${CERTS}/${service}/server.crt" -days "${DAYS}" -sha256 \
    -extfile <(printf 'subjectAltName=DNS:%s,DNS:localhost,IP:127.0.0.1\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n' "${service}") \
    2>/dev/null

  rm -f "${CERTS}/${service}/server.csr"
  cp "${CERTS}/ca/ca.crt" "${CERTS}/${service}/ca.crt"

  # Readable by the container user, whichever uid the image happens to use.
  # Postgres is the exception and re-installs its key as 0600 in its entrypoint.
  chmod 644 "${CERTS}/${service}/server.key" "${CERTS}/${service}/server.crt"
done

chmod 600 "${CERTS}/ca/ca.key"
chmod 644 "${CERTS}/ca/ca.crt"

echo
echo "Development certificates written to ${CERTS}."
echo "CA: ${CERTS}/ca/ca.crt"
