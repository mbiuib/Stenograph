#!/usr/bin/env bash
# Issue a TLS certificate for the local Jitsi deployment (ASCII only).
#
# Usage:  make_cert.sh <IP> [hostname]
#
# Creates a local CA if it does not exist yet and writes:
#   ca/ca.crt + ca/ca.key                 - the CA (give ca.crt to clients)
#   config/web/keys/cert.crt + cert.key   - the certificate Jitsi will serve
#                                           (full chain: leaf + CA)
# Re-run with a new IP to re-issue; delete ca/ to start a new CA.
set -euo pipefail
cd "$(dirname "$0")"

IP="${1:-127.0.0.1}"
HOST="${2:-}"

OSS="${OPENSSL_BIN:-openssl}"
command -v "$OSS" >/dev/null 2>&1 || { echo "openssl not found in PATH"; exit 1; }

mkdir -p ca config/web/keys

if [ ! -f ca/ca.crt ]; then
    "$OSS" req -x509 -newkey rsa:2048 -nodes -days 1825 -sha256 \
        -keyout ca/ca.key -out ca/ca.crt \
        -subj "/O=Stenograph Dev/CN=Stenograph Local CA"
    echo "new CA created: ca/ca.crt"
fi

SAN="IP:${IP},IP:127.0.0.1,DNS:localhost"
if [ -n "$HOST" ]; then SAN="$SAN,DNS:$HOST"; fi

# NOTE: keep the extension file in the current directory under a RELATIVE
# path - OpenSSL on Git for Windows cannot read MSYS absolute paths like
# /tmp/... (mktemp output).
EXT="cert.ext.$$"
trap 'rm -f "$EXT"' EXIT
printf 'subjectAltName=%s\nbasicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n' "$SAN" > "$EXT"

"$OSS" req -new -newkey rsa:2048 -nodes \
    -keyout config/web/keys/cert.key -out config/web/keys/cert.csr -subj "/CN=${IP}"
"$OSS" x509 -req -in config/web/keys/cert.csr -CA ca/ca.crt -CAkey ca/ca.key \
    -CAcreateserial -out config/web/keys/cert.crt -days 825 -sha256 -extfile "$EXT"
cat ca/ca.crt >> config/web/keys/cert.crt
rm -f config/web/keys/cert.csr ca/ca.srl

echo "ok: config/web/keys/cert.crt (SAN: $SAN)"
echo "client CA to share: ca/ca.crt"
