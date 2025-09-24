#!/bin/bash
# Deployment script for the modern FastAPI forex bot

set -e

echo "🚀 Starting deployment of Forex Bot v2.0..."

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Function to print colored output
print_status() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if .env file exists
if [ ! -f ".env" ]; then
    print_warning ".env file not found. Creating from example..."
    cp env.example .env
    print_warning "Please edit .env file with your configuration before continuing."
    exit 1
fi

# Load environment variables
source .env

# Check required environment variables
required_vars=("TELEGRAM_BOT_TOKEN" "SECURITY_SECRET_KEY")
for var in "${required_vars[@]}"; do
    if [ -z "${!var}" ]; then
        print_error "Required environment variable $var is not set"
        exit 1
    fi
done

print_status "Environment variables validated"

# Build and start services
print_status "Building Docker images..."
docker-compose build

print_status "Starting services..."
docker-compose up -d

# Wait for services to be healthy
print_status "Waiting for services to be healthy..."
sleep 30

# Check service health
print_status "Checking service health..."

# Check PostgreSQL
if docker-compose exec -T db pg_isready -U $DB_USER -d $DB_NAME > /dev/null 2>&1; then
    print_status "PostgreSQL is healthy"
else
    print_error "PostgreSQL is not healthy"
    exit 1
fi

# Check Redis
if docker-compose exec -T redis redis-cli ping > /dev/null 2>&1; then
    print_status "Redis is healthy"
else
    print_error "Redis is not healthy"
    exit 1
fi

# Check FastAPI app
if curl -f http://localhost:8000/health > /dev/null 2>&1; then
    print_status "FastAPI application is healthy"
else
    print_error "FastAPI application is not healthy"
    exit 1
fi

# Run database migrations
print_status "Running database migrations..."
docker-compose exec app python scripts/migrate.py

print_status "Deployment completed successfully! 🎉"

echo ""
echo "📊 Service URLs:"
echo "  - FastAPI App: http://localhost:8000"
echo "  - API Docs: http://localhost:8000/docs"
echo "  - Health Check: http://localhost:8000/health"
echo "  - Flower (Celery): http://localhost:5555"
echo ""
echo "📝 Next steps:"
echo "  1. Set up your Telegram webhook: POST http://localhost:8000/api/v1/telegram/webhook/set"
echo "  2. Configure your domain and SSL certificates"
echo "  3. Set up monitoring and logging"
echo "  4. Configure backup strategies"
echo ""
echo "🔧 Useful commands:"
echo "  - View logs: docker-compose logs -f"
echo "  - Stop services: docker-compose down"
echo "  - Restart app: docker-compose restart app"
