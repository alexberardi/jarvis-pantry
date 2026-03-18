#!/bin/bash
# Production server
# Usage: ./run-prod.sh [--build]

set -e
cd "$(dirname "$0")"

BUILD_FLAGS=""
if [[ "$1" == "--build" ]]; then
    BUILD_FLAGS="--build"
fi

docker compose --env-file .env -f docker-compose.prod.yaml up -d $BUILD_FLAGS

echo "jarvis-pantry running in production mode"
echo "Logs: docker compose -f docker-compose.prod.yaml logs -f"
echo "Stop: docker compose -f docker-compose.prod.yaml down"
