# Forex News Bot - Modern FastAPI Application

A modern, production-ready Telegram bot for Forex news with AI analysis and chart generation, built with FastAPI and following Context7 best practices.

## 🚀 Features

### Core Functionality
- **Real-time Forex News**: Scrape and analyze forex news from multiple sources
- **AI-Powered Analysis**: GPT-4 integration for intelligent market analysis
- **Interactive Charts**: Generate candlestick charts with event annotations
- **Smart Notifications**: Customizable notifications based on impact levels
- **Multi-Currency Support**: Support for major currencies and cryptocurrencies
- **Timezone Awareness**: Proper timezone handling for global users

### Technical Features
- **Modern FastAPI Architecture**: High-performance async API
- **Pydantic V2 Models**: Type-safe data validation and serialization
- **SQLAlchemy 2.0**: Modern async ORM with proper relationships
- **Structured Logging**: JSON logging with correlation IDs
- **Comprehensive Testing**: Unit, integration, and performance tests
- **Docker Support**: Production-ready containerization
- **Redis Caching**: High-performance caching layer
- **Rate Limiting**: Built-in API rate limiting
- **Health Monitoring**: Comprehensive health checks

## 📁 Project Structure

```
forex_to_telegram/
├── app/                          # Main application package
│   ├── __init__.py
│   ├── main.py                   # FastAPI application entry point
│   ├── core/                     # Core application modules
│   │   ├── __init__.py
│   │   ├── config.py             # Pydantic settings
│   │   ├── exceptions.py         # Custom exceptions
│   │   └── logging.py            # Structured logging
│   ├── database/                 # Database layer
│   │   ├── __init__.py
│   │   ├── connection.py         # Database connection management
│   │   └── models.py             # SQLAlchemy models
│   ├── models/                   # Pydantic models
│   │   ├── __init__.py
│   │   ├── user.py               # User models
│   │   ├── forex_news.py         # Forex news models
│   │   ├── chart.py              # Chart models
│   │   ├── notification.py       # Notification models
│   │   └── telegram.py           # Telegram models
│   ├── services/                 # Business logic layer
│   │   ├── __init__.py
│   │   ├── base.py               # Base service class
│   │   ├── user_service.py       # User business logic
│   │   ├── forex_service.py      # Forex news logic
│   │   ├── chart_service.py      # Chart generation
│   │   ├── notification_service.py # Notification logic
│   │   ├── telegram_service.py   # Telegram bot logic
│   │   └── scraping_service.py   # Web scraping
│   └── api/                      # API layer
│       ├── __init__.py
│       └── v1/                   # API version 1
│           ├── __init__.py
│           ├── router.py         # Main API router
│           └── endpoints/        # API endpoints
│               ├── __init__.py
│               ├── users.py      # User endpoints
│               ├── forex_news.py # Forex news endpoints
│               ├── charts.py     # Chart endpoints
│               ├── notifications.py # Notification endpoints
│               └── telegram.py   # Telegram webhook endpoints
├── tests/                        # Test suite
│   ├── __init__.py
│   ├── conftest.py               # Pytest configuration
│   ├── test_core/                # Core module tests
│   │   ├── test_config.py
│   │   └── test_exceptions.py
│   └── test_api/                 # API tests
│       └── test_users.py
├── migrations/                   # Database migrations
├── scripts/                      # Utility scripts
├── requirements.txt              # Python dependencies
├── pytest.ini                   # Pytest configuration
├── docker-compose.yml           # Docker Compose setup
├── Dockerfile                   # Docker configuration
├── env.example                  # Environment variables template
└── README.md                    # This file
```

## 🛠️ Installation

### Prerequisites
- Python 3.11+
- PostgreSQL 14+ (or SQLite for development)
- Redis 6+ (optional, for caching)
- Docker & Docker Compose (optional)

### Local Development

1. **Clone the repository**
   ```bash
   git clone <repository-url>
   cd forex_to_telegram
   ```

2. **Create virtual environment**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Set up environment variables**
   ```bash
   cp env.example .env
   # Edit .env with your configuration
   ```

5. **Initialize database**
   ```bash
   # For SQLite (development)
   python -c "from app.database.connection import db_manager; import asyncio; asyncio.run(db_manager.initialize())"

   # For PostgreSQL (production)
   alembic upgrade head
   ```

6. **Run the application**
   ```bash
   uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
   ```

### Docker Deployment

1. **Build and run with Docker Compose**
   ```bash
   docker-compose up -d
   ```

2. **View logs**
   ```bash
   docker-compose logs -f app
   ```

## 🔧 Configuration

### Environment Variables

Create a `.env` file based on `env.example`:

```bash
# Application
ENVIRONMENT=development
DEBUG=true
APP_NAME=Forex News Bot
APP_VERSION=2.0.0

# Server
SERVER_HOST=0.0.0.0
SERVER_PORT=8000

# Database
DB_URL=sqlite+aiosqlite:///./forex_bot.db
# DB_URL=postgresql+asyncpg://user:password@localhost/forex_bot

# Redis
REDIS_URL=redis://localhost:6379

# Telegram
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_WEBHOOK_URL=https://yourdomain.com/api/v1/telegram/webhook
TELEGRAM_WEBHOOK_SECRET=your_secret_token

# API Keys
API_OPENAI_API_KEY=your_openai_api_key
API_ALPHA_VANTAGE_KEY=your_alpha_vantage_key

# Security
SECURITY_SECRET_KEY=your_secret_key_minimum_32_characters
```

### Configuration Classes

The application uses Pydantic Settings for type-safe configuration:

- **DatabaseSettings**: Database connection and pool settings
- **RedisSettings**: Redis connection and caching settings
- **TelegramSettings**: Telegram bot configuration
- **APISettings**: External API keys and settings
- **ChartSettings**: Chart generation settings
- **SecuritySettings**: Security and authentication settings
- **LoggingSettings**: Logging configuration
- **ServerSettings**: Server and deployment settings

## 🧪 Testing

### Running Tests

```bash
# Run all tests
pytest

# Run specific test categories
pytest tests/test_core/          # Unit tests
pytest tests/test_api/           # API tests
pytest -m integration            # Integration tests
pytest -m "not slow"            # Skip slow tests

# Run with coverage
pytest --cov=app --cov-report=html

# Run specific test file
pytest tests/test_api/test_users.py -v
```

### Test Structure

- **Unit Tests**: Test individual components in isolation
- **Integration Tests**: Test component interactions
- **API Tests**: Test HTTP endpoints and responses
- **Performance Tests**: Test response times and load handling

### Test Fixtures

- **Database**: In-memory SQLite for fast testing
- **HTTP Client**: Async HTTP client for API testing
- **Sample Data**: Predefined test data fixtures
- **Mocks**: External service mocks

## 📊 API Documentation

### Interactive Documentation

Once the application is running, visit:
- **Swagger UI**: http://localhost:8000/docs
- **ReDoc**: http://localhost:8000/redoc

### API Endpoints

#### Users
- `POST /api/v1/users/` - Create user
- `GET /api/v1/users/{telegram_id}` - Get user
- `PUT /api/v1/users/{telegram_id}` - Update user
- `PUT /api/v1/users/{telegram_id}/preferences` - Update preferences
- `GET /api/v1/users/` - List users
- `GET /api/v1/users/by-currency/{currency}` - Get users by currency
- `GET /api/v1/users/by-impact/{impact_level}` - Get users by impact level

#### Forex News
- `POST /api/v1/forex-news/` - Create news
- `GET /api/v1/forex-news/{news_id}` - Get news
- `PUT /api/v1/forex-news/{news_id}` - Update news
- `GET /api/v1/forex-news/` - List news
- `GET /api/v1/forex-news/by-date/{date}` - Get news by date
- `GET /api/v1/forex-news/by-currency/{currency}` - Get news by currency
- `GET /api/v1/forex-news/upcoming/` - Get upcoming news
- `GET /api/v1/forex-news/search/` - Search news

#### Charts
- `POST /api/v1/charts/generate` - Generate chart
- `POST /api/v1/charts/generate/image` - Generate chart image
- `GET /api/v1/charts/currencies/` - Get supported currencies
- `GET /api/v1/charts/health` - Chart service health

#### Notifications
- `POST /api/v1/notifications/` - Create notification
- `GET /api/v1/notifications/{notification_id}` - Get notification
- `GET /api/v1/notifications/` - List notifications
- `GET /api/v1/notifications/pending/` - Get pending notifications
- `GET /api/v1/notifications/due/` - Get due notifications
- `POST /api/v1/notifications/{notification_id}/mark-sent` - Mark as sent
- `POST /api/v1/notifications/{notification_id}/mark-failed` - Mark as failed

#### Telegram
- `POST /api/v1/telegram/webhook` - Telegram webhook
- `GET /api/v1/telegram/webhook-info` - Get webhook info
- `POST /api/v1/telegram/setup-webhook` - Setup webhook
- `DELETE /api/v1/telegram/webhook` - Delete webhook
- `POST /api/v1/telegram/test-message` - Send test message

## 🤖 Telegram Bot Commands

- `/start` - Welcome message and bot introduction
- `/help` - Show available commands
- `/settings` - Configure user preferences
- `/news` - Get latest forex news
- `/currencies` - Manage currency preferences
- `/impact` - Set impact level preferences
- `/digest` - Configure daily digest
- `/charts` - Enable/disable charts
- `/status` - Check current settings
- `/support` - Get support information

## 🏗️ Architecture

### Design Patterns

- **Service Layer Pattern**: Business logic separated from API layer
- **Repository Pattern**: Data access abstraction
- **Dependency Injection**: FastAPI's built-in DI system
- **Factory Pattern**: Service instantiation
- **Observer Pattern**: Event-driven notifications

### Data Flow

1. **Telegram Webhook** → **API Endpoint** → **Service Layer** → **Database**
2. **External APIs** → **Scraping Service** → **Forex Service** → **Database**
3. **Scheduled Tasks** → **Notification Service** → **Telegram Service** → **Users**

### Error Handling

- **Custom Exceptions**: Domain-specific error types
- **Global Exception Handlers**: Centralized error processing
- **Structured Logging**: Comprehensive error tracking
- **Graceful Degradation**: Fallback mechanisms

## 🚀 Deployment

### Production Checklist

- [ ] Set `ENVIRONMENT=production`
- [ ] Set `DEBUG=false`
- [ ] Configure production database
- [ ] Set up Redis caching
- [ ] Configure Telegram webhook
- [ ] Set up monitoring and logging
- [ ] Configure SSL/TLS
- [ ] Set up backup strategy
- [ ] Configure rate limiting
- [ ] Set up health checks

### Docker Deployment

```bash
# Build production image
docker build -t forex-bot:latest .

# Run with Docker Compose
docker-compose -f docker-compose.prod.yml up -d

# Scale services
docker-compose up -d --scale app=3
```

### Environment-Specific Configurations

- **Development**: SQLite, debug enabled, detailed logging
- **Staging**: PostgreSQL, limited debug, structured logging
- **Production**: PostgreSQL, no debug, JSON logging, monitoring

## 📈 Monitoring & Observability

### Health Checks

- `GET /health` - Application health status
- `GET /api/v1/charts/health` - Chart service health
- Database connection health
- Redis connection health
- External API health

### Logging

- **Structured Logging**: JSON format for production
- **Correlation IDs**: Request tracing across services
- **Log Levels**: DEBUG, INFO, WARNING, ERROR, CRITICAL
- **Log Rotation**: Automatic log file rotation

### Metrics

- Request/response times
- Error rates
- Database query performance
- External API response times
- Memory and CPU usage

## 🔒 Security

### Authentication & Authorization

- JWT token-based authentication
- Role-based access control
- API key authentication for internal services
- Telegram webhook secret validation

### Data Protection

- Input validation with Pydantic
- SQL injection prevention with SQLAlchemy
- XSS protection with proper escaping
- CSRF protection for web endpoints
- Rate limiting to prevent abuse

### Secrets Management

- Environment variables for sensitive data
- Docker secrets for containerized deployments
- Separate configuration for different environments
- Regular secret rotation

## 🤝 Contributing

### Development Workflow

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests for new functionality
5. Ensure all tests pass
6. Submit a pull request

### Code Standards

- **Black**: Code formatting
- **isort**: Import sorting
- **flake8**: Linting
- **mypy**: Type checking
- **pytest**: Testing

### Commit Convention

```
feat: add new feature
fix: bug fix
docs: documentation changes
style: formatting changes
refactor: code refactoring
test: test additions/changes
chore: maintenance tasks
```

## 📄 License

This project is licensed under the MIT License - see the LICENSE file for details.

## 🆘 Support

- **Documentation**: Check this README and API docs
- **Issues**: Create GitHub issues for bugs and feature requests
- **Discussions**: Use GitHub Discussions for questions
- **Email**: support@forexbot.com

## 🎯 Roadmap

### Upcoming Features

- [ ] Advanced chart analysis
- [ ] Machine learning predictions
- [ ] Multi-language support
- [ ] Mobile app integration
- [ ] Advanced notification scheduling
- [ ] Social trading features
- [ ] Portfolio tracking
- [ ] Risk management tools

### Performance Improvements

- [ ] Database query optimization
- [ ] Caching layer enhancement
- [ ] Async processing improvements
- [ ] CDN integration for charts
- [ ] Database sharding
- [ ] Microservices architecture

---

**Built with ❤️ using FastAPI, Pydantic, SQLAlchemy, and modern Python practices.**
