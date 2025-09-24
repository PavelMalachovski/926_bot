"""User API endpoints."""

from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import get_database
from app.services.user_service import UserService
from app.models.user import User, UserCreate, UserUpdate, UserPreferences, UserResponse
from app.core.exceptions import ValidationError, DatabaseError

router = APIRouter()


def get_user_service() -> UserService:
    """Get user service instance."""
    return UserService()


@router.post("/", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    user_data: UserCreate,
    db: AsyncSession = Depends(get_database),
    user_service: UserService = Depends(get_user_service)
):
    """Create a new user."""
    try:
        user = await user_service.create_user(db, user_data)
        return UserResponse.model_validate(user)
    except ValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except DatabaseError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get("/{telegram_id}", response_model=UserResponse)
async def get_user(
    telegram_id: int,
    db: AsyncSession = Depends(get_database),
    user_service: UserService = Depends(get_user_service)
):
    """Get user by Telegram ID."""
    try:
        user = await user_service.get_by_telegram_id(db, telegram_id)
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        return UserResponse.model_validate(user)
    except DatabaseError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.put("/{telegram_id}", response_model=UserResponse)
async def update_user(
    telegram_id: int,
    user_data: UserUpdate,
    db: AsyncSession = Depends(get_database),
    user_service: UserService = Depends(get_user_service)
):
    """Update user by Telegram ID."""
    try:
        user = await user_service.update_user(db, telegram_id, user_data)
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        return UserResponse.model_validate(user)
    except ValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except DatabaseError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.put("/{telegram_id}/preferences", response_model=UserResponse)
async def update_user_preferences(
    telegram_id: int,
    preferences: UserPreferences,
    db: AsyncSession = Depends(get_database),
    user_service: UserService = Depends(get_user_service)
):
    """Update user preferences."""
    try:
        user = await user_service.update_preferences(db, telegram_id, preferences)
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        return UserResponse.model_validate(user)
    except ValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except DatabaseError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get("/", response_model=List[UserResponse])
async def get_users(
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(100, ge=1, le=1000, description="Number of records to return"),
    is_active: Optional[bool] = Query(None, description="Filter by active status"),
    db: AsyncSession = Depends(get_database),
    user_service: UserService = Depends(get_user_service)
):
    """Get all users with optional filtering."""
    try:
        filters = {}
        if is_active is not None:
            filters["is_active"] = is_active

        users = await user_service.get_all(db, skip=skip, limit=limit, filters=filters)
        return [UserResponse.model_validate(user) for user in users]
    except DatabaseError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get("/by-currency/{currency}", response_model=List[UserResponse])
async def get_users_by_currency(
    currency: str,
    db: AsyncSession = Depends(get_database),
    user_service: UserService = Depends(get_user_service)
):
    """Get users by preferred currency."""
    try:
        users = await user_service.get_users_by_currency(db, currency)
        return [UserResponse.model_validate(user) for user in users]
    except DatabaseError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get("/by-impact/{impact_level}", response_model=List[UserResponse])
async def get_users_by_impact_level(
    impact_level: str,
    db: AsyncSession = Depends(get_database),
    user_service: UserService = Depends(get_user_service)
):
    """Get users by impact level preference."""
    try:
        users = await user_service.get_users_by_impact_level(db, impact_level)
        return [UserResponse.model_validate(user) for user in users]
    except DatabaseError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.post("/{telegram_id}/update-activity")
async def update_user_activity(
    telegram_id: int,
    db: AsyncSession = Depends(get_database),
    user_service: UserService = Depends(get_user_service)
):
    """Update user's last active timestamp."""
    try:
        success = await user_service.update_last_active(db, telegram_id)
        if not success:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        return {"message": "Activity updated successfully"}
    except DatabaseError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.delete("/{telegram_id}")
async def deactivate_user(
    telegram_id: int,
    db: AsyncSession = Depends(get_database),
    user_service: UserService = Depends(get_user_service)
):
    """Deactivate a user."""
    try:
        success = await user_service.deactivate_user(db, telegram_id)
        if not success:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        return {"message": "User deactivated successfully"}
    except DatabaseError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
