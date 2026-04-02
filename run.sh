#!/bin/bash
# Development server with hot reload
# Usage: ./run.sh [--docker [--build|--rebuild]]

set -e
cd "$(dirname "$0")"

if [[ "$1" == "--docker" ]]; then
    # Docker development mode
    BUILD_FLAGS=""
    if [[ "$2" == "--rebuild" || "$2" == "--build" ]]; then
        # Copy SDK into build context (Dockerfile needs it, but it lives outside)
        cp -r ../jarvis-command-sdk jarvis-command-sdk
        trap "rm -rf jarvis-command-sdk" EXIT
        if [[ "$2" == "--rebuild" ]]; then
            docker compose --env-file .env -f docker-compose.dev.yaml build --no-cache
        fi
        BUILD_FLAGS="--build"
    fi
    docker compose --env-file .env -f docker-compose.dev.yaml up $BUILD_FLAGS -d
else
    # Local development mode
    set -a
    source .env
    set +a

    # Activate venv if it exists
    if [ -d ".venv" ]; then
        source .venv/bin/activate
    fi

    uvicorn app.main:app --host 0.0.0.0 --port ${PANTRY_PORT:-7720} --reload
fi
