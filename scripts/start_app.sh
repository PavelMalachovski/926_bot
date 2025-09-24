#!/bin/bash
# Startup script for Forex Bot application with Redis initialization

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Logging functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Configuration
REDIS_HOST=${REDIS_HOST:-redis}
REDIS_PORT=${REDIS_PORT:-6379}
REDIS_PASSWORD=${REDIS_PASSWORD:-}
REDIS_DB=${REDIS_DB:-0}
REDIS_MAX_RETRIES=${REDIS_MAX_RETRIES:-30}
REDIS_RETRY_DELAY=${REDIS_RETRY_DELAY:-2}

APP_HOST=${APP_HOST:-0.0.0.0}
APP_PORT=${APP_PORT:-8000}
APP_WORKERS=${APP_WORKERS:-1}

log_info "Starting Forex Bot application with Redis integration"
log_info "Redis: ${REDIS_HOST}:${REDIS_PORT}"
log_info "App: ${APP_HOST}:${APP_PORT}"

# Function to check if Redis is available
check_redis() {
    log_info "Checking Redis availability..."

    if command -v redis-cli >/dev/null 2>&1; then
        if redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" ping >/dev/null 2>&1; then
            log_success "Redis is available"
            return 0
        else
            log_warning "Redis is not responding"
            return 1
        fi
    else
        log_warning "redis-cli not available, skipping Redis check"
        return 0
    fi
}

# Function to wait for Redis
wait_for_redis() {
    log_info "Waiting for Redis to be ready..."

    python scripts/wait_for_redis.py

    if [ $? -eq 0 ]; then
        log_success "Redis is ready"
    else
        log_error "Redis failed to become ready"
        exit 1
    fi
}

# Function to run database migrations
run_migrations() {
    log_info "Running database migrations..."

    if [ -f "alembic.ini" ]; then
        alembic upgrade head
        if [ $? -eq 0 ]; then
            log_success "Database migrations completed"
        else
            log_error "Database migrations failed"
            exit 1
        fi
    else
        log_warning "No alembic.ini found, skipping migrations"
    fi
}

# Function to start the application
start_app() {
    log_info "Starting FastAPI application..."

    # Set environment variables for Redis
    export REDIS_HOST="$REDIS_HOST"
    export REDIS_PORT="$REDIS_PORT"
    export REDIS_PASSWORD="$REDIS_PASSWORD"
    export REDIS_DB="$REDIS_DB"

    # Start the application
    if [ "$APP_WORKERS" -gt 1 ]; then
        log_info "Starting with $APP_WORKERS workers"
        uvicorn app.main:app \
            --host "$APP_HOST" \
            --port "$APP_PORT" \
            --workers "$APP_WORKERS" \
            --access-log \
            --log-level info
    else
        log_info "Starting with single worker"
        uvicorn app.main:app \
            --host "$APP_HOST" \
            --port "$APP_PORT" \
            --access-log \
            --log-level info \
            --reload
    fi
}

# Function to handle graceful shutdown
cleanup() {
    log_info "Shutting down gracefully..."
    exit 0
}

# Set up signal handlers
trap cleanup SIGTERM SIGINT

# Main execution
main() {
    log_info "Forex Bot startup sequence initiated"

    # Check Redis availability
    if ! check_redis; then
        log_warning "Redis check failed, but continuing..."
    fi

    # Wait for Redis to be ready
    wait_for_redis

    # Run database migrations
    run_migrations

    # Start the application
    start_app
}

# Run main function
main "$@"
