#!/bin/sh
set -e

if [ "$#" -gt 0 ] && [ "$1" != "app" ] && [ "$1" != "worker" ] && [ "$1" != "background" ]; then
  exec "$@"
fi

mode="${MAKERHUB_ENTRYPOINT:-${MAKERHUB_PROCESS_ROLE:-${MAKERHUB_ROLE:-${1:-app}}}}"
mode="$(printf '%s' "$mode" | tr '[:upper:]' '[:lower:]')"

case "$mode" in
  worker|background)
    exec python -m app.worker
    ;;
  app|api|web|legacy|all|"")
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000
    ;;
  *)
    echo "Unknown MAKERHUB_ENTRYPOINT: $mode" >&2
    exit 64
    ;;
esac
