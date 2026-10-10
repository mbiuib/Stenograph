#!/usr/bin/env bash
# Выпуск сертификата для «Стенографа» из локального CA — без правки кода.
#
# Использование:  scripts/make_cert.sh [IP] [имя-хоста]
#   scripts/make_cert.sh                  # IP по умолчанию 192.168.0.9
#   scripts/make_cert.sh 10.0.0.5 host.lan
#
# CA:        $STENOGRAPH_CA_DIR (по умолчанию ~/stenograph-ca, файлы ca.crt+ca.key)
# Результат: certs/stenograph.crt + certs/stenograph.key (папка certs в .gitignore)
set -euo pipefail
cd "$(dirname "$0")/.."

IP="${1:-192.168.0.9}"
HOST="${2:-}"
CA_DIR="${STENOGRAPH_CA_DIR:-$HOME/stenograph-ca}"

# Нативный OpenSSL из Git for Windows не понимает POSIX-пути (/c/...):
# конвертируем в смешанный вид C:/... (на Linux cygpath отсутствует — no-op).
if command -v cygpath >/dev/null 2>&1; then
  CA_DIR="$(cygpath -m "$CA_DIR")"
fi

SAN="IP:${IP},IP:127.0.0.1,DNS:localhost,DNS:stenograph.local,DNS:host.docker.internal"
if [ -n "$HOST" ]; then
  SAN="$SAN,DNS:$HOST"
fi

# ext-файл держим рядом с CWD относительным путём: OpenSSL из Git for Windows
# не умеет читать MSYS-пути вида /tmp/... (вывод mktemp).
EXT=".cert-ext.$$"
trap 'rm -f "$EXT"' EXIT
printf 'subjectAltName=%s\nbasicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n' "$SAN" > "$EXT"

mkdir -p certs
openssl req -new -newkey rsa:2048 -nodes \
  -keyout certs/stenograph.key -out certs/stenograph.csr -subj "/CN=${IP}"
openssl x509 -req -in certs/stenograph.csr -CA "$CA_DIR/ca.crt" -CAkey "$CA_DIR/ca.key" \
  -CAcreateserial -out certs/stenograph.crt -days 825 -sha256 -extfile "$EXT"
rm -f certs/stenograph.csr

echo "готово: certs/stenograph.crt (SAN: $SAN)"
echo "перезапусти сервер (start_server.bat), чтобы он подхватил новый сертификат"
