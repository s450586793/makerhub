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
    workers="${MAKERHUB_WEB_WORKERS:-1}"
    case "$workers" in
      ''|*[!0-9]*)
        workers=1
        ;;
    esac
    if [ "$workers" -lt 1 ]; then
      workers=1
    elif [ "$workers" -gt 8 ]; then
      workers=8
    fi
    trusted_proxies="$(printf '%s' "${MAKERHUB_TRUSTED_PROXIES:-}" | tr -d '[:space:]')"
    trusted_proxies_valid=true
    old_ifs="$IFS"
    IFS=','
    set -f
    for trusted_proxy in $trusted_proxies; do
      case "$trusted_proxy" in
        ""|*[*]*) trusted_proxies_valid=false ;;
        */*)
          prefix_length="${trusted_proxy##*/}"
          case "$prefix_length" in
            ""|*[!0-9]*) ;;
            *)
              if [ "$prefix_length" -eq 0 ]; then
                trusted_proxies_valid=false
              fi
              ;;
          esac
          ;;
      esac
    done
    IFS="$old_ifs"
    set +f
    if [ -z "$trusted_proxies" ] || [ "$trusted_proxies_valid" != true ]; then
      exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers "$workers" --no-proxy-headers
    fi
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers "$workers" \
      --proxy-headers --forwarded-allow-ips "$trusted_proxies"
    ;;
  *)
    echo "Unknown MAKERHUB_ENTRYPOINT: $mode" >&2
    exit 64
    ;;
esac
