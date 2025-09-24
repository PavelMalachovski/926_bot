-- Initialize database for modern FastAPI application
-- This script runs when the PostgreSQL container starts for the first time

-- Create database if it doesn't exist
SELECT 'CREATE DATABASE forex_bot'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'forex_bot')\gexec

-- Connect to the forex_bot database
\c forex_bot;

-- Create extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- Create indexes for better performance
-- These will be created by Alembic migrations, but we can add some initial ones here

-- Grant permissions
GRANT ALL PRIVILEGES ON DATABASE forex_bot TO forex_user;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO forex_user;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO forex_user;
