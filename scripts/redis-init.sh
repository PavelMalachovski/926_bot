#!/bin/bash
# Redis initialization script for Docker

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

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

# Wait for Redis to be ready
wait_for_redis() {
    log_info "Waiting for Redis to be ready..."

    local max_attempts=30
    local attempt=1

    while [ $attempt -le $max_attempts ]; do
        if redis-cli ping >/dev/null 2>&1; then
            log_success "Redis is ready!"
            return 0
        fi

        log_info "Attempt $attempt/$max_attempts - Redis not ready yet..."
        sleep 2
        attempt=$((attempt + 1))
    done

    log_error "Redis failed to become ready within timeout"
    return 1
}

# Initialize Redis with application-specific settings
initialize_redis() {
    log_info "Initializing Redis for Forex Bot application..."

    # Set memory policy
    redis-cli CONFIG SET maxmemory-policy allkeys-lru
    log_success "Set maxmemory-policy to allkeys-lru"

    # Enable keyspace notifications
    redis-cli CONFIG SET notify-keyspace-events Ex
    log_success "Enabled keyspace notifications"

    # Set client timeout
    redis-cli CONFIG SET timeout 300
    log_success "Set client timeout to 300 seconds"

    # Create initial namespaces (optional)
    redis-cli SET "app:initialized" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    log_success "Created application initialization marker"

    # Set up initial cache keys for monitoring
    redis-cli SET "cache:stats:hits" "0"
    redis-cli SET "cache:stats:misses" "0"
    redis-cli SET "cache:stats:sets" "0"
    redis-cli SET "cache:stats:deletes" "0"
    log_success "Initialized cache statistics keys"

    # Create session namespace marker
    redis-cli SET "session:namespace:initialized" "true"
    log_success "Initialized session namespace"

    # Create rate limit namespace marker
    redis-cli SET "rate_limit:namespace:initialized" "true"
    log_success "Initialized rate limit namespace"

    # Create pubsub namespace marker
    redis-cli SET "pubsub:namespace:initialized" "true"
    log_success "Initialized pubsub namespace"
}

# Optimize Redis for production
optimize_redis() {
    log_info "Optimizing Redis for production..."

    # Set appropriate memory limits
    redis-cli CONFIG SET maxmemory 256mb
    log_success "Set maxmemory to 256mb"

    # Optimize hash settings
    redis-cli CONFIG SET hash-max-ziplist-entries 512
    redis-cli CONFIG SET hash-max-ziplist-value 64
    log_success "Optimized hash settings"

    # Optimize list settings
    redis-cli CONFIG SET list-max-ziplist-size -2
    redis-cli CONFIG SET list-compress-depth 0
    log_success "Optimized list settings"

    # Optimize set settings
    redis-cli CONFIG SET set-max-intset-entries 512
    log_success "Optimized set settings"

    # Optimize sorted set settings
    redis-cli CONFIG SET zset-max-ziplist-entries 128
    redis-cli CONFIG SET zset-max-ziplist-value 64
    log_success "Optimized sorted set settings"

    # Enable active rehashing
    redis-cli CONFIG SET activerehashing yes
    log_success "Enabled active rehashing"
}

# Create Redis monitoring keys
setup_monitoring() {
    log_info "Setting up Redis monitoring..."

    # Create monitoring keys
    redis-cli SET "monitoring:startup_time" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    redis-cli SET "monitoring:version" "1.0.0"
    redis-cli SET "monitoring:environment" "${ENVIRONMENT:-development}"

    # Create health check key
    redis-cli SET "health:redis" "ok" EX 300

    log_success "Redis monitoring setup completed"
}

# Main initialization function
main() {
    log_info "Starting Redis initialization for Forex Bot application"

    # Wait for Redis to be ready
    if ! wait_for_redis; then
        log_error "Failed to connect to Redis"
        exit 1
    fi

    # Initialize Redis
    initialize_redis

    # Optimize Redis for production
    if [ "${ENVIRONMENT:-development}" = "production" ]; then
        optimize_redis
    fi

    # Setup monitoring
    setup_monitoring

    log_success "Redis initialization completed successfully!"
}

# Run main function
main "$@"
