"""Notification API endpoints."""

from typing import List, Optional
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import get_database
from app.services.notification_service import NotificationService
from app.models.notification import Notification, NotificationCreate, NotificationResponse
from app.core.exceptions import ValidationError, DatabaseError

router = APIRouter()


def get_notification_service() -> NotificationService:
    """Get notification service instance."""
    return NotificationService()


@router.post("/", response_model=NotificationResponse, status_code=status.HTTP_201_CREATED)
async def create_notification(
    notification_data: NotificationCreate,
    db: AsyncSession = Depends(get_database),
    notification_service: NotificationService = Depends(get_notification_service)
):
    """Create a new notification."""
    try:
        notification = await notification_service.create_notification(db, notification_data)
        return NotificationResponse.model_validate(notification)
    except ValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except DatabaseError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get("/{notification_id}", response_model=NotificationResponse)
async def get_notification(
    notification_id: int,
    db: AsyncSession = Depends(get_database),
    notification_service: NotificationService = Depends(get_notification_service)
):
    """Get notification by ID."""
    try:
        notification = await notification_service.get(db, notification_id)
        if not notification:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")
        return NotificationResponse.model_validate(notification)
    except DatabaseError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get("/", response_model=List[NotificationResponse])
async def get_notifications(
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(100, ge=1, le=1000, description="Number of records to return"),
    status: Optional[str] = Query(None, description="Filter by status"),
    notification_type: Optional[str] = Query(None, description="Filter by notification type"),
    db: AsyncSession = Depends(get_database),
    notification_service: NotificationService = Depends(get_notification_service)
):
    """Get notifications with optional filtering."""
    try:
        filters = {}
        if status:
            filters["status"] = status
        if notification_type:
            filters["notification_type"] = notification_type

        notifications = await notification_service.get_all(db, skip=skip, limit=limit, filters=filters)
        return [NotificationResponse.model_validate(notification) for notification in notifications]
    except DatabaseError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get("/user/{user_id}", response_model=List[NotificationResponse])
async def get_notifications_by_user(
    user_id: int,
    db: AsyncSession = Depends(get_database),
    notification_service: NotificationService = Depends(get_notification_service)
):
    """Get notifications for a specific user."""
    try:
        notifications = await notification_service.get_notifications_by_user(db, user_id)
        return [NotificationResponse.model_validate(notification) for notification in notifications]
    except DatabaseError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get("/status/{status}", response_model=List[NotificationResponse])
async def get_notifications_by_status(
    status: str,
    db: AsyncSession = Depends(get_database),
    notification_service: NotificationService = Depends(get_notification_service)
):
    """Get notifications by status."""
    try:
        notifications = await notification_service.get_notifications_by_status(db, status)
        return [NotificationResponse.model_validate(notification) for notification in notifications]
    except DatabaseError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get("/pending/", response_model=List[NotificationResponse])
async def get_pending_notifications(
    db: AsyncSession = Depends(get_database),
    notification_service: NotificationService = Depends(get_notification_service)
):
    """Get all pending notifications."""
    try:
        notifications = await notification_service.get_pending_notifications(db)
        return [NotificationResponse.model_validate(notification) for notification in notifications]
    except DatabaseError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get("/due/", response_model=List[NotificationResponse])
async def get_due_notifications(
    db: AsyncSession = Depends(get_database),
    notification_service: NotificationService = Depends(get_notification_service)
):
    """Get notifications that are due to be sent."""
    try:
        notifications = await notification_service.get_due_notifications(db)
        return [NotificationResponse.model_validate(notification) for notification in notifications]
    except DatabaseError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.post("/{notification_id}/mark-sent")
async def mark_notification_sent(
    notification_id: int,
    db: AsyncSession = Depends(get_database),
    notification_service: NotificationService = Depends(get_notification_service)
):
    """Mark notification as sent."""
    try:
        success = await notification_service.mark_notification_sent(db, notification_id)
        if not success:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")
        return {"message": "Notification marked as sent"}
    except DatabaseError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.post("/{notification_id}/mark-failed")
async def mark_notification_failed(
    notification_id: int,
    error_message: str,
    db: AsyncSession = Depends(get_database),
    notification_service: NotificationService = Depends(get_notification_service)
):
    """Mark notification as failed."""
    try:
        success = await notification_service.mark_notification_failed(db, notification_id, error_message)
        if not success:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")
        return {"message": "Notification marked as failed"}
    except DatabaseError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.post("/{notification_id}/cancel")
async def cancel_notification(
    notification_id: int,
    db: AsyncSession = Depends(get_database),
    notification_service: NotificationService = Depends(get_notification_service)
):
    """Cancel a notification."""
    try:
        success = await notification_service.cancel_notification(db, notification_id)
        if not success:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")
        return {"message": "Notification cancelled"}
    except DatabaseError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.post("/event-reminder/")
async def create_event_reminder(
    user_id: int,
    event_id: int,
    event_time: datetime,
    minutes_before: int = Query(30, ge=5, le=120, description="Minutes before event"),
    db: AsyncSession = Depends(get_database),
    notification_service: NotificationService = Depends(get_notification_service)
):
    """Create an event reminder notification."""
    try:
        notification = await notification_service.create_event_reminder(
            db, user_id, event_id, event_time, minutes_before
        )
        return NotificationResponse.model_validate(notification)
    except ValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except DatabaseError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.post("/digest/")
async def create_digest_notification(
    user_id: int,
    digest_time: datetime,
    db: AsyncSession = Depends(get_database),
    notification_service: NotificationService = Depends(get_notification_service)
):
    """Create a daily digest notification."""
    try:
        notification = await notification_service.create_digest_notification(db, user_id, digest_time)
        return NotificationResponse.model_validate(notification)
    except ValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except DatabaseError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get("/statistics/", response_model=dict)
async def get_notification_statistics(
    db: AsyncSession = Depends(get_database),
    notification_service: NotificationService = Depends(get_notification_service)
):
    """Get notification statistics."""
    try:
        stats = await notification_service.get_notification_statistics(db)
        return stats
    except DatabaseError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.delete("/{notification_id}")
async def delete_notification(
    notification_id: int,
    db: AsyncSession = Depends(get_database),
    notification_service: NotificationService = Depends(get_notification_service)
):
    """Delete a notification."""
    try:
        success = await notification_service.delete(db, notification_id)
        if not success:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")
        return {"message": "Notification deleted successfully"}
    except DatabaseError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
