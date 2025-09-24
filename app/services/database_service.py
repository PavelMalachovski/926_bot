"""Advanced database service with schema evolution handling for Render.com PostgreSQL."""

from datetime import datetime, date
from typing import List, Optional, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import and_, func, text
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from app.database.connection import db_manager
from app.database.models import UserModel, ForexNewsModel
from app.models.user import UserCreate, UserUpdate
from app.models.forex_news import ForexNewsCreate
from app.core.exceptions import DatabaseError, ValidationError

logger = structlog.get_logger(__name__)


class DatabaseService:
    """Advanced database service with schema evolution handling."""

    def __init__(self):
        self.db_manager = db_manager

    async def get_or_create_user(self, telegram_id: int) -> UserModel:
        """Get existing user or create a new one with schema evolution handling."""
        try:
            async with self.db_manager.get_session_async() as session:
                # Check if all required columns exist (notification + chart + timezone)
                result = await session.execute(text("""
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_name = 'users' AND column_name IN (
                        'notifications_enabled', 'notification_minutes', 'notification_impact_levels',
                        'charts_enabled', 'chart_type', 'chart_window_hours', 'timezone'
                    )
                """))
                all_columns = [row[0] for row in result]

                # Check if we have all required columns
                required_columns = [
                    'notifications_enabled', 'notification_minutes', 'notification_impact_levels',
                    'charts_enabled', 'chart_type', 'chart_window_hours', 'timezone'
                ]

                if all(col in all_columns for col in required_columns):
                    # All required columns exist, use normal query
                    user = await session.get(UserModel, telegram_id)
                    if not user:
                        user = UserModel(telegram_id=telegram_id)
                        session.add(user)
                        await session.commit()
                        logger.info("Created new user", telegram_id=telegram_id)
                    return user
                else:
                    # Some columns missing, use raw SQL for getting user
                    base_columns = ['id', 'telegram_id', 'preferred_currencies', 'impact_levels',
                                  'analysis_required', 'digest_time', 'created_at', 'updated_at']

                    # Add optional columns only if they exist
                    optional_columns = [
                        'notifications_enabled', 'notification_minutes', 'notification_impact_levels',
                        'charts_enabled', 'chart_type', 'chart_window_hours', 'timezone'
                    ]

                    columns = base_columns.copy()
                    for col in optional_columns:
                        if col in all_columns:
                            columns.append(col)

                    columns_str = ', '.join([f'users.{col} AS users_{col}' for col in columns])
                    sql = f"SELECT {columns_str} FROM users WHERE telegram_id = :telegram_id LIMIT 1"

                    result = await session.execute(text(sql), {'telegram_id': telegram_id})
                    row = result.fetchone()

                    if row:
                        # User exists, create User object manually
                        user_data = {}
                        for i, col in enumerate(columns):
                            user_data[col] = row[i]

                        user = UserModel()
                        user.id = user_data['id']
                        user.telegram_id = user_data['telegram_id']
                        user.preferred_currencies = user_data['preferred_currencies']
                        user.impact_levels = user_data['impact_levels']
                        user.analysis_required = user_data['analysis_required']
                        user.digest_time = user_data['digest_time']
                        user.created_at = user_data['created_at']
                        user.updated_at = user_data['updated_at']

                        # Set optional fields only if they exist
                        for field in optional_columns:
                            if field in user_data:
                                setattr(user, field, user_data[field])

                        return user
                    else:
                        # User doesn't exist, create new user with raw SQL
                        insert_columns = ['telegram_id', 'created_at', 'updated_at']
                        insert_values = [':telegram_id', 'NOW()', 'NOW()']

                        insert_sql = f"""
                            INSERT INTO users ({', '.join(insert_columns)})
                            VALUES ({', '.join(insert_values)})
                            RETURNING id, telegram_id, preferred_currencies, impact_levels,
                                     analysis_required, digest_time, created_at, updated_at
                        """

                        result = await session.execute(text(insert_sql), {'telegram_id': telegram_id})
                        row = result.fetchone()
                        await session.commit()

                        # Create User object manually
                        user = UserModel()
                        user.id = row[0]
                        user.telegram_id = row[1]
                        user.preferred_currencies = row[2]
                        user.impact_levels = row[3]
                        user.analysis_required = row[4]
                        user.digest_time = row[5]
                        user.created_at = row[6]
                        user.updated_at = row[7]

                        logger.info("Created new user", telegram_id=telegram_id)
                        return user

        except Exception as e:
            logger.error("Error getting/creating user", telegram_id=telegram_id, error=str(e))
            raise

    async def update_user_preferences(self, telegram_id: int, **kwargs) -> bool:
        """Update user preferences with schema evolution handling."""
        try:
            async with self.db_manager.get_session_async() as session:
                # Check if all required columns exist (notification + chart + timezone)
                result = await session.execute(text("""
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_name = 'users' AND column_name IN (
                        'notifications_enabled', 'notification_minutes', 'notification_impact_levels',
                        'charts_enabled', 'chart_type', 'chart_window_hours', 'timezone'
                    )
                """))
                all_columns = [row[0] for row in result]

                # Check if we have all required columns
                required_columns = [
                    'notifications_enabled', 'notification_minutes', 'notification_impact_levels',
                    'charts_enabled', 'chart_type', 'chart_window_hours', 'timezone'
                ]

                if all(col in all_columns for col in required_columns):
                    # All required columns exist, use normal query
                    user = await session.get(UserModel, telegram_id)
                    if not user:
                        return False

                    for key, value in kwargs.items():
                        if hasattr(user, key):
                            setattr(user, key, value)
                        else:
                            # Handle optional fields that might not exist in database yet
                            if key in required_columns:
                                # Skip updating these fields if they don't exist in the database
                                continue

                    await session.commit()
                    logger.info("Updated preferences for user", telegram_id=telegram_id)
                    return True
                else:
                    # Some columns missing, use raw SQL
                    # First get the user to check if it exists
                    base_columns = ['id', 'telegram_id', 'preferred_currencies', 'impact_levels',
                                  'analysis_required', 'digest_time', 'created_at', 'updated_at']

                    # Add optional columns only if they exist
                    optional_columns = [
                        'notifications_enabled', 'notification_minutes', 'notification_impact_levels',
                        'charts_enabled', 'chart_type', 'chart_window_hours', 'timezone'
                    ]

                    columns = base_columns.copy()
                    for col in optional_columns:
                        if col in all_columns:
                            columns.append(col)

                    columns_str = ', '.join([f'users.{col} AS users_{col}' for col in columns])
                    sql = f"SELECT {columns_str} FROM users WHERE telegram_id = :telegram_id LIMIT 1"

                    result = await session.execute(text(sql), {'telegram_id': telegram_id})
                    row = result.fetchone()

                    if not row:
                        return False

                    # Build UPDATE statement with only existing columns
                    update_parts = []
                    update_values = {'telegram_id': telegram_id}

                    for key, value in kwargs.items():
                        if key in columns:
                            update_parts.append(f"{key} = :{key}")
                            update_values[key] = value
                        elif key in ['notifications_enabled', 'notification_minutes', 'notification_impact_levels', 'timezone', 'charts_enabled', 'chart_type', 'chart_window_hours']:
                            # Skip notification and chart fields that don't exist
                            continue

                    if update_parts:
                        update_sql = f"""
                            UPDATE users
                            SET {', '.join(update_parts)}, updated_at = NOW()
                            WHERE telegram_id = :telegram_id
                        """

                        await session.execute(text(update_sql), update_values)
                        await session.commit()
                        logger.info("Updated preferences for user", telegram_id=telegram_id)
                        return True
                    else:
                        logger.info("No valid fields to update for user", telegram_id=telegram_id)
                        return True

        except Exception as e:
            logger.error("Error updating user preferences", telegram_id=telegram_id, error=str(e))
            return False

    async def get_user_by_telegram_id(self, db: AsyncSession, telegram_id: int) -> Optional[UserModel]:
        """Get user by telegram ID with schema evolution handling."""
        try:
            # Check if all required columns exist (notification + chart + timezone)
            result = await db.execute(text("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'users' AND column_name IN (
                    'notifications_enabled', 'notification_minutes', 'notification_impact_levels',
                    'charts_enabled', 'chart_type', 'chart_window_hours', 'timezone'
                )
            """))
            all_columns = [row[0] for row in await result.fetchall()]

            # Check if we have all required columns
            required_columns = [
                'notifications_enabled', 'notification_minutes', 'notification_impact_levels',
                'charts_enabled', 'chart_type', 'chart_window_hours', 'timezone'
            ]

            if all(col in all_columns for col in required_columns):
                # All required columns exist, use normal query
                return await db.get(UserModel, telegram_id)
            else:
                # Some columns missing, use raw SQL
                base_columns = ['id', 'telegram_id', 'preferred_currencies', 'impact_levels',
                              'analysis_required', 'digest_time', 'created_at', 'updated_at']

                # Add optional columns only if they exist
                optional_columns = [
                    'notifications_enabled', 'notification_minutes', 'notification_impact_levels',
                    'charts_enabled', 'chart_type', 'chart_window_hours', 'timezone'
                ]

                columns = base_columns.copy()
                for col in optional_columns:
                    if col in all_columns:
                        columns.append(col)

                columns_str = ', '.join([f'users.{col} AS users_{col}' for col in columns])
                sql = f"SELECT {columns_str} FROM users WHERE telegram_id = :telegram_id LIMIT 1"

                result = await db.execute(text(sql), {'telegram_id': telegram_id})
                row = await result.fetchone()

                if row:
                    user_data = {}
                    for i, col in enumerate(columns):
                        user_data[col] = row[i]

                    # Create User object manually
                    user = UserModel()
                    user.id = user_data['id']
                    user.telegram_id = user_data['telegram_id']
                    user.preferred_currencies = user_data['preferred_currencies']
                    user.impact_levels = user_data['impact_levels']
                    user.analysis_required = user_data['analysis_required']
                    user.digest_time = user_data['digest_time']
                    user.created_at = user_data['created_at']
                    user.updated_at = user_data['updated_at']

                    # Set notification fields only if they exist
                    if 'notifications_enabled' in user_data:
                        user.notifications_enabled = user_data['notifications_enabled']
                    if 'notification_minutes' in user_data:
                        user.notification_minutes = user_data['notification_minutes']
                    if 'notification_impact_levels' in user_data:
                        user.notification_impact_levels = user_data['notification_impact_levels']

                    return user

            return None
        except Exception as e:
            logger.error("Error getting user by telegram ID", telegram_id=telegram_id, error=str(e))
            return None

    async def get_user_preferences(self, telegram_id: int) -> Optional[Dict[str, Any]]:
        """Get user preferences by telegram ID with schema evolution handling."""
        try:
            async with self.db_manager.get_session_async() as session:
                # Check if all required columns exist (notification + chart + timezone)
                result = await session.execute(text("""
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_name = 'users' AND column_name IN (
                        'notifications_enabled', 'notification_minutes', 'notification_impact_levels',
                        'charts_enabled', 'chart_type', 'chart_window_hours', 'timezone'
                    )
                """))
                all_columns = [row[0] for row in result]

                # Check if we have all required columns
                required_columns = [
                    'notifications_enabled', 'notification_minutes', 'notification_impact_levels',
                    'charts_enabled', 'chart_type', 'chart_window_hours', 'timezone'
                ]

                if all(col in all_columns for col in required_columns):
                    # All required columns exist, use normal query
                    user = await session.get(UserModel, telegram_id)
                    if user:
                        return user.to_dict()
                else:
                    # Some columns missing, use raw SQL
                    base_columns = ['id', 'telegram_id', 'preferred_currencies', 'impact_levels',
                                  'analysis_required', 'digest_time', 'created_at', 'updated_at']

                    # Add optional columns only if they exist
                    optional_columns = [
                        'notifications_enabled', 'notification_minutes', 'notification_impact_levels',
                        'charts_enabled', 'chart_type', 'chart_window_hours', 'timezone'
                    ]

                    columns = base_columns.copy()
                    for col in optional_columns:
                        if col in all_columns:
                            columns.append(col)

                    columns_str = ', '.join([f'users.{col} AS users_{col}' for col in columns])
                    sql = f"SELECT {columns_str} FROM users WHERE telegram_id = :telegram_id LIMIT 1"

                    result = await session.execute(text(sql), {'telegram_id': telegram_id})
                    row = result.fetchone()

                    if row:
                        user_data = {}
                        for i, col in enumerate(columns):
                            user_data[col] = row[i]

                        # Create User object manually
                        user = UserModel()
                        user.id = user_data['id']
                        user.telegram_id = user_data['telegram_id']
                        user.preferred_currencies = user_data['preferred_currencies']
                        user.impact_levels = user_data['impact_levels']
                        user.analysis_required = user_data['analysis_required']
                        user.digest_time = user_data['digest_time']
                        user.created_at = user_data['created_at']
                        user.updated_at = user_data['updated_at']

                        # Set optional fields only if they exist
                        for field in optional_columns:
                            if field in user_data:
                                setattr(user, field, user_data[field])

                        return user.to_dict()

                return None
        except Exception as e:
            logger.error("Error getting user preferences", telegram_id=telegram_id, error=str(e))
            return None

    async def get_users_for_digest(self, digest_time: datetime.time) -> List[UserModel]:
        """Get all users who should receive digest at the specified time."""
        try:
            async with self.db_manager.get_session_async() as session:
                result = await session.execute(
                    text("SELECT * FROM users WHERE digest_time = :digest_time"),
                    {'digest_time': digest_time}
                )
                users = []
                for row in result:
                    user = UserModel()
                    # Map row data to user object
                    # This is a simplified version - in practice you'd map all fields
                    user.id = row[0]
                    user.telegram_id = row[1]
                    # ... map other fields
                    users.append(user)
                return users
        except Exception as e:
            logger.error("Error getting users for digest", digest_time=digest_time, error=str(e))
            return []

    async def get_all_users(self) -> List[UserModel]:
        """Get all users with schema evolution handling."""
        try:
            async with self.db_manager.get_session_async() as session:
                # Check if all required columns exist (notification + chart + timezone)
                result = await session.execute(text("""
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_name = 'users' AND column_name IN (
                        'notifications_enabled', 'notification_minutes', 'notification_impact_levels',
                        'charts_enabled', 'chart_type', 'chart_window_hours', 'timezone'
                    )
                """))
                all_columns = [row[0] for row in result]

                # Check if we have all required columns
                required_columns = [
                    'notifications_enabled', 'notification_minutes', 'notification_impact_levels',
                    'charts_enabled', 'chart_type', 'chart_window_hours', 'timezone'
                ]

                if all(col in all_columns for col in required_columns):
                    # All required columns exist, use normal query
                    result = await session.execute(text("SELECT * FROM users"))
                    users = []
                    for row in result:
                        user = UserModel()
                        # Map row data to user object
                        # This is a simplified version - in practice you'd map all fields
                        user.id = row[0]
                        user.telegram_id = row[1]
                        # ... map other fields
                        users.append(user)
                    return users
                else:
                    # Some columns missing, use raw SQL
                    base_columns = ['id', 'telegram_id', 'preferred_currencies', 'impact_levels',
                                  'analysis_required', 'digest_time', 'created_at', 'updated_at']

                    # Add optional columns only if they exist
                    optional_columns = [
                        'notifications_enabled', 'notification_minutes', 'notification_impact_levels',
                        'charts_enabled', 'chart_type', 'chart_window_hours', 'timezone'
                    ]

                    columns = base_columns.copy()
                    for col in optional_columns:
                        if col in all_columns:
                            columns.append(col)

                    columns_str = ', '.join([f'users.{col} AS users_{col}' for col in columns])
                    sql = f"SELECT {columns_str} FROM users"

                    result = await session.execute(text(sql))
                    users = []
                    for row in result:
                        user_data = {}
                        for i, col in enumerate(columns):
                            user_data[col] = row[i]

                        # Create User object manually
                        user = UserModel()
                        user.id = user_data['id']
                        user.telegram_id = user_data['telegram_id']
                        user.preferred_currencies = user_data['preferred_currencies']
                        user.impact_levels = user_data['impact_levels']
                        user.analysis_required = user_data['analysis_required']
                        user.digest_time = user_data['digest_time']
                        user.created_at = user_data['created_at']
                        user.updated_at = user_data['updated_at']

                        # Set optional fields only if they exist
                        for field in optional_columns:
                            if field in user_data:
                                setattr(user, field, user_data[field])

                        users.append(user)

                    return users
        except Exception as e:
            logger.error("Error getting all users", error=str(e))
            return []

    async def get_users_with_notifications_enabled(self) -> List[UserModel]:
        """Get all users who have notifications enabled with schema evolution handling."""
        try:
            async with self.db_manager.get_session_async() as session:
                # Check if all required columns exist (notification + chart + timezone)
                result = await session.execute(text("""
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_name = 'users' AND column_name IN (
                        'notifications_enabled', 'notification_minutes', 'notification_impact_levels',
                        'charts_enabled', 'chart_type', 'chart_window_hours', 'timezone'
                    )
                """))
                all_columns = [row[0] for row in result]

                if 'notifications_enabled' not in all_columns:
                    # Notification columns don't exist, return empty list
                    logger.info("Notification columns not found, returning empty list")
                    return []

                # Check if we have all required columns
                required_columns = [
                    'notifications_enabled', 'notification_minutes', 'notification_impact_levels',
                    'charts_enabled', 'chart_type', 'chart_window_hours', 'timezone'
                ]

                if all(col in all_columns for col in required_columns):
                    # All required columns exist, use normal query
                    result = await session.execute(text("SELECT * FROM users WHERE notifications_enabled = TRUE"))
                    users = []
                    for row in result:
                        user = UserModel()
                        # Map row data to user object
                        # This is a simplified version - in practice you'd map all fields
                        user.id = row[0]
                        user.telegram_id = row[1]
                        # ... map other fields
                        users.append(user)
                    return users
                else:
                    # Some columns missing, use raw SQL
                    base_columns = ['id', 'telegram_id', 'preferred_currencies', 'impact_levels',
                                  'analysis_required', 'digest_time', 'created_at', 'updated_at']

                    # Add optional columns only if they exist
                    optional_columns = [
                        'notifications_enabled', 'notification_minutes', 'notification_impact_levels',
                        'charts_enabled', 'chart_type', 'chart_window_hours', 'timezone'
                    ]

                    columns = base_columns.copy()
                    for col in optional_columns:
                        if col in all_columns:
                            columns.append(col)

                    columns_str = ', '.join([f'users.{col} AS users_{col}' for col in columns])
                    sql = f"SELECT {columns_str} FROM users WHERE notifications_enabled = TRUE"

                    result = await session.execute(text(sql))
                    users = []
                    for row in result:
                        user_data = {}
                        for i, col in enumerate(columns):
                            user_data[col] = row[i]

                        # Create User object manually
                        user = UserModel()
                        user.id = user_data['id']
                        user.telegram_id = user_data['telegram_id']
                        user.preferred_currencies = user_data['preferred_currencies']
                        user.impact_levels = user_data['impact_levels']
                        user.analysis_required = user_data['analysis_required']
                        user.digest_time = user_data['digest_time']
                        user.created_at = user_data['created_at']
                        user.updated_at = user_data['updated_at']

                        # Set optional fields only if they exist
                        for field in optional_columns:
                            if field in user_data:
                                setattr(user, field, user_data[field])

                        users.append(user)

                    return users
        except Exception as e:
            logger.error("Error getting users with notifications enabled", error=str(e))
            return []

    async def get_news_for_date(self, target_date: date, impact_level: str = "high") -> List[Dict[str, Any]]:
        """Get news for a specific date from the database."""
        try:
            async with self.db_manager.get_session_async() as session:
                # Convert date to datetime for comparison
                start_datetime = datetime.combine(target_date, datetime.min.time())
                end_datetime = datetime.combine(target_date, datetime.max.time())

                query_sql = """
                    SELECT * FROM forex_news
                    WHERE date >= :start_datetime AND date <= :end_datetime
                """
                params = {'start_datetime': start_datetime, 'end_datetime': end_datetime}

                # Filter by impact level if specified
                if impact_level != "all":
                    query_sql += " AND impact_level = :impact_level"
                    params['impact_level'] = impact_level

                # Order by currency and time
                query_sql += " ORDER BY currency, time"

                result = await session.execute(text(query_sql), params)
                news_items = result.fetchall()

                # Convert to list of dictionaries
                result_list = []
                for item in news_items:
                    news_dict = {
                        "time": item.time,
                        "currency": item.currency,
                        "event": item.event,
                        "actual": item.actual or "N/A",
                        "forecast": item.forecast or "N/A",
                        "previous": item.previous or "N/A",
                        "analysis": item.analysis or None,
                        "impact": item.impact_level,  # Add this line for Telegram output
                        "impact_level": item.impact_level,  # Keep for DB/API compatibility
                        "group_analysis": False,  # DB does not store this, set default
                    }
                    result_list.append(news_dict)

                logger.info("Retrieved news items", count=len(result_list), date=target_date, impact_level=impact_level)
                return result_list

        except Exception as e:
            logger.error("Error retrieving news for date", date=target_date, error=str(e))
            return []

    async def has_news_for_date(self, target_date: date, impact_level: str = "high") -> bool:
        """Check if news exists for a specific date."""
        try:
            async with self.db_manager.get_session_async() as session:
                start_datetime = datetime.combine(target_date, datetime.min.time())
                end_datetime = datetime.combine(target_date, datetime.max.time())

                query_sql = """
                    SELECT COUNT(*) FROM forex_news
                    WHERE date >= :start_datetime AND date <= :end_datetime
                """
                params = {'start_datetime': start_datetime, 'end_datetime': end_datetime}

                if impact_level != "all":
                    query_sql += " AND impact_level = :impact_level"
                    params['impact_level'] = impact_level

                result = await session.execute(text(query_sql), params)
                count = result.scalar()
                return count > 0

        except Exception as e:
            logger.error("Error checking news existence for date", date=target_date, error=str(e))
            return False

    async def store_news_items(self, news_items: List[Dict[str, Any]], target_date: date, impact_level: str = "high") -> bool:
        """Store news items in the database."""
        try:
            async with self.db_manager.get_session_async() as session:
                # Delete existing news for this date. If impact_level == 'all', remove all rows for the date
                # to avoid duplicates across repeated imports.
                start_datetime = datetime.combine(target_date, datetime.min.time())
                end_datetime = datetime.combine(target_date, datetime.max.time())

                delete_sql = """
                    DELETE FROM forex_news
                    WHERE date >= :start_datetime AND date <= :end_datetime
                """
                params = {'start_datetime': start_datetime, 'end_datetime': end_datetime}

                if impact_level != "all":
                    delete_sql += " AND impact_level = :impact_level"
                    params['impact_level'] = impact_level

                await session.execute(text(delete_sql), params)

                # Insert new news items
                for item in news_items:
                    insert_sql = """
                        INSERT INTO forex_news (date, time, currency, event, actual, forecast, previous, impact_level, analysis)
                        VALUES (:date, :time, :currency, :event, :actual, :forecast, :previous, :impact_level, :analysis)
                    """
                    await session.execute(text(insert_sql), {
                        'date': target_date,
                        'time': item.get("time", "N/A"),
                        'currency': item.get("currency", "N/A"),
                        'event': item.get("event", "N/A"),
                        'actual': item.get("actual", "N/A"),
                        'forecast': item.get("forecast", "N/A"),
                        'previous': item.get("previous", "N/A"),
                        'impact_level': item.get("impact", impact_level),  # Use per-item impact if present
                        'analysis': item.get("analysis", None)
                    })

                await session.commit()
                logger.info("Stored news items", count=len(news_items), date=target_date, impact_level=impact_level)
                return True

        except Exception as e:
            logger.error("Error storing news for date", date=target_date, error=str(e))
            return False

    async def get_date_range_stats(self, start_date: date, end_date: date) -> Dict[str, Any]:
        """Get statistics for a date range."""
        try:
            async with self.db_manager.get_session_async() as session:
                start_datetime = datetime.combine(start_date, datetime.min.time())
                end_datetime = datetime.combine(end_date, datetime.max.time())

                # Get total count
                total_count_sql = """
                    SELECT COUNT(*) FROM forex_news
                    WHERE date >= :start_datetime AND date <= :end_datetime
                """
                result = await session.execute(text(total_count_sql), {'start_datetime': start_datetime, 'end_datetime': end_datetime})
                total_count = result.scalar()

                # Get count by impact level
                impact_stats_sql = """
                    SELECT impact_level, COUNT(*)
                    FROM forex_news
                    WHERE date >= :start_datetime AND date <= :end_datetime
                    GROUP BY impact_level
                """
                result = await session.execute(text(impact_stats_sql), {'start_datetime': start_datetime, 'end_datetime': end_datetime})
                impact_stats = dict(result.fetchall())

                # Get count by currency
                currency_stats_sql = """
                    SELECT currency, COUNT(*)
                    FROM forex_news
                    WHERE date >= :start_datetime AND date <= :end_datetime
                    GROUP BY currency
                """
                result = await session.execute(text(currency_stats_sql), {'start_datetime': start_datetime, 'end_datetime': end_datetime})
                currency_stats = dict(result.fetchall())

                return {
                    "total_news": total_count,
                    "impact_levels": impact_stats,
                    "currencies": currency_stats,
                    "date_range": {
                        "start": start_date.isoformat(),
                        "end": end_date.isoformat()
                    }
                }

        except Exception as e:
            logger.error("Error getting stats for date range", start_date=start_date, end_date=end_date, error=str(e))
            return {}

    async def health_check(self) -> bool:
        """Check if the database service is healthy."""
        try:
            async with self.db_manager.get_session_async() as session:
                await session.execute(text("SELECT 1"))
                return True
        except Exception as e:
            logger.error("Database health check failed", error=str(e))
            return False

    # ============================================================================
    # MISSING SERVICE METHODS FOR TEST COMPATIBILITY
    # ============================================================================

    async def create_user(self, db: AsyncSession, user_data: UserCreate) -> UserModel:
        """Create a new user."""
        try:
            user_dict = user_data.model_dump()

            # Extract preferences and map to individual fields
            preferences = user_dict.pop('preferences', {})
            if preferences:
                # Map preferences to individual fields
                user_dict.update({
                    'preferred_currencies': preferences.get('preferred_currencies', []),
                    'impact_levels': preferences.get('impact_levels', ["high", "medium"]),
                    'analysis_required': preferences.get('analysis_required', True),
                    'digest_time': preferences.get('digest_time', "08:00:00"),
                    'timezone': preferences.get('timezone', "Europe/Prague"),
                    'notifications_enabled': preferences.get('notifications_enabled', False),
                    'notification_minutes': preferences.get('notification_minutes', 30),
                    'notification_impact_levels': preferences.get('notification_impact_levels', ["high"]),
                    'charts_enabled': preferences.get('charts_enabled', False),
                    'chart_type': preferences.get('chart_type', "single"),
                    'chart_window_hours': preferences.get('chart_window_hours', 2),
                })

            user = UserModel(**user_dict)
            db.add(user)
            await db.commit()
            await db.refresh(user)
            logger.info("Created new user", user_id=user.id, telegram_id=user.telegram_id)
            return user
        except Exception as e:
            await db.rollback()
            logger.error("Failed to create user", error=str(e), exc_info=True)
            raise DatabaseError(f"Failed to create user: {e}")

    async def update_user(self, db: AsyncSession, telegram_id: int, update_data: UserUpdate) -> Optional[UserModel]:
        """Update user by telegram ID."""
        try:
            user = await self.get_user_by_telegram_id(db, telegram_id)
            if not user:
                raise ValidationError("User not found")

            if hasattr(update_data, 'model_dump'):
                update_dict = update_data.model_dump(exclude_unset=True)
            else:
                update_dict = update_data

            # Handle preferences separately
            preferences = update_dict.pop('preferences', None)
            if preferences:
                # Map preferences to individual fields
                preference_fields = {
                    'preferred_currencies': preferences.get('preferred_currencies'),
                    'impact_levels': preferences.get('impact_levels'),
                    'analysis_required': preferences.get('analysis_required'),
                    'digest_time': preferences.get('digest_time'),
                    'timezone': preferences.get('timezone'),
                    'notifications_enabled': preferences.get('notifications_enabled'),
                    'notification_minutes': preferences.get('notification_minutes'),
                    'notification_impact_levels': preferences.get('notification_impact_levels'),
                    'charts_enabled': preferences.get('charts_enabled'),
                    'chart_type': preferences.get('chart_type'),
                    'chart_window_hours': preferences.get('chart_window_hours'),
                }
                # Only update fields that are not None
                for key, value in preference_fields.items():
                    if value is not None:
                        update_dict[key] = value

            for key, value in update_dict.items():
                if hasattr(user, key):
                    setattr(user, key, value)

            await db.commit()
            await db.refresh(user)
            logger.info("Updated user", telegram_id=telegram_id)
            return user
        except ValidationError:
            raise
        except Exception as e:
            await db.rollback()
            logger.error("Failed to update user", telegram_id=telegram_id, error=str(e), exc_info=True)
            raise DatabaseError(f"Failed to update user: {e}")

    async def create_forex_news(self, db: AsyncSession, news_data: ForexNewsCreate) -> ForexNewsModel:
        """Create new forex news."""
        try:
            news_dict = news_data.model_dump()
            news = ForexNewsModel(**news_dict)
            db.add(news)
            await db.commit()
            await db.refresh(news)
            logger.info("Created new forex news", news_id=news.id)
            return news
        except Exception as e:
            await db.rollback()
            logger.error("Failed to create forex news", error=str(e), exc_info=True)
            raise DatabaseError(f"Failed to create forex news: {e}")

    async def get_forex_news_by_date(self, db: AsyncSession, target_date: date) -> List[ForexNewsModel]:
        """Get forex news by date."""
        try:
            start_datetime = datetime.combine(target_date, datetime.min.time())
            end_datetime = datetime.combine(target_date, datetime.max.time())

            result = await db.execute(text("""
                SELECT * FROM forex_news
                WHERE date >= :start_datetime AND date <= :end_datetime
                ORDER BY time
            """), {'start_datetime': start_datetime, 'end_datetime': end_datetime})

            rows = await result.fetchall()
            news_items = []
            for row in rows:
                news = ForexNewsModel()
                # Map row data to ForexNewsModel
                news.id = row[0]
                news.date = row[1]
                news.time = row[2]
                news.currency = row[3]
                news.event = row[4]
                news.actual = row[5]
                news.forecast = row[6]
                news.previous = row[7]
                news.impact_level = row[8]
                news.analysis = row[9]
                news.created_at = row[10]
                news.updated_at = row[11]
                news_items.append(news)

            return news_items
        except Exception as e:
            logger.error("Failed to get forex news by date", date=target_date, error=str(e), exc_info=True)
            raise DatabaseError(f"Failed to get forex news by date: {e}")

    async def get_forex_news_by_currency(self, db: AsyncSession, currency: str) -> List[ForexNewsModel]:
        """Get forex news by currency."""
        try:
            result = await db.execute(text("""
                SELECT * FROM forex_news
                WHERE currency = :currency
                ORDER BY date DESC, time DESC
            """), {'currency': currency})

            rows = await result.fetchall()
            news_items = []
            for row in rows:
                news = ForexNewsModel()
                # Map row data to ForexNewsModel
                news.id = row[0]
                news.date = row[1]
                news.time = row[2]
                news.currency = row[3]
                news.event = row[4]
                news.actual = row[5]
                news.forecast = row[6]
                news.previous = row[7]
                news.impact_level = row[8]
                news.analysis = row[9]
                news.created_at = row[10]
                news.updated_at = row[11]
                news_items.append(news)

            return news_items
        except Exception as e:
            logger.error("Failed to get forex news by currency", currency=currency, error=str(e), exc_info=True)
            raise DatabaseError(f"Failed to get forex news by currency: {e}")

    async def get_users_by_currency(self, db: AsyncSession, currency: str) -> List[UserModel]:
        """Get users who have a specific currency in their preferences."""
        try:
            result = await db.execute(text("""
                SELECT * FROM users
                WHERE preferred_currencies @> :currency_array
            """), {'currency_array': [currency]})

            rows = await result.fetchall()
            users = []
            for row in rows:
                user = UserModel()
                # Map row data to UserModel
                user.id = row[0]
                user.telegram_id = row[1]
                user.preferred_currencies = row[2]
                user.impact_levels = row[3]
                user.analysis_required = row[4]
                user.digest_time = row[5]
                user.created_at = row[6]
                user.updated_at = row[7]
                users.append(user)

            return users
        except Exception as e:
            logger.error("Failed to get users by currency", currency=currency, error=str(e), exc_info=True)
            raise DatabaseError(f"Failed to get users by currency: {e}")

    async def get_users_by_impact_level(self, db: AsyncSession, impact_level: str) -> List[UserModel]:
        """Get users who have a specific impact level in their preferences."""
        try:
            result = await db.execute(text("""
                SELECT * FROM users
                WHERE impact_levels @> :impact_array
            """), {'impact_array': [impact_level]})

            rows = await result.fetchall()
            users = []
            for row in rows:
                user = UserModel()
                # Map row data to UserModel
                user.id = row[0]
                user.telegram_id = row[1]
                user.preferred_currencies = row[2]
                user.impact_levels = row[3]
                user.analysis_required = row[4]
                user.digest_time = row[5]
                user.created_at = row[6]
                user.updated_at = row[7]
                users.append(user)

            return users
        except Exception as e:
            logger.error("Failed to get users by impact level", impact_level=impact_level, error=str(e), exc_info=True)
            raise DatabaseError(f"Failed to get users by impact level: {e}")
