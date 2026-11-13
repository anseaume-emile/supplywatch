#!/bin/sh
set -e
# One worker: the scheduler and job lock live in-process.
exec uvicorn supplywatch.main:app --host 0.0.0.0 --port 8080 --workers 1 --proxy-headers --forwarded-allow-ips "*" "$@"
