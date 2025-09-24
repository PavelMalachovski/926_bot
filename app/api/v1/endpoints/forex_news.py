"""Forex news API endpoints."""

from typing import List, Optional
from datetime import datetime, date
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import get_database
from app.services.forex_service import ForexService
from app.models.forex_news import ForexNews, ForexNewsCreate, ForexNewsUpdate, ForexNewsResponse
from app.core.exceptions import ValidationError, DatabaseError

router = APIRouter()


def get_forex_service() -> ForexService:
    """Get forex service instance."""
    return ForexService()


@router.post("/", response_model=ForexNewsResponse, status_code=status.HTTP_201_CREATED)
async def create_forex_news(
    news_data: ForexNewsCreate,
    db: AsyncSession = Depends(get_database),
    forex_service: ForexService = Depends(get_forex_service)
):
    """Create new forex news."""
    try:
        news = await forex_service.create_news(db, news_data)
        return ForexNewsResponse.model_validate(news)
    except ValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except DatabaseError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get("/upcoming", response_model=List[ForexNewsResponse])
async def get_upcoming_events(
    hours: int = Query(24, ge=1, le=168, description="Hours ahead to look for upcoming events"),
    db: AsyncSession = Depends(get_database),
    forex_service: ForexService = Depends(get_forex_service)
):
    """Get upcoming events."""
    try:
        news_list = await forex_service.get_upcoming_events(db, hours)
        return [ForexNewsResponse.model_validate(news) for news in news_list]
    except DatabaseError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get("/{news_id}", response_model=ForexNewsResponse)
async def get_forex_news(
    news_id: int,
    db: AsyncSession = Depends(get_database),
    forex_service: ForexService = Depends(get_forex_service)
):
    """Get forex news by ID."""
    try:
        news = await forex_service.get(db, news_id)
        if not news:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Forex news not found")
        return ForexNewsResponse.model_validate(news)
    except DatabaseError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.put("/{news_id}", response_model=ForexNewsResponse)
async def update_forex_news(
    news_id: int,
    news_data: ForexNewsUpdate,
    db: AsyncSession = Depends(get_database),
    forex_service: ForexService = Depends(get_forex_service)
):
    """Update forex news."""
    try:
        news = await forex_service.update_news(db, news_id, news_data)
        if not news:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Forex news not found")
        return ForexNewsResponse.model_validate(news)
    except ValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except DatabaseError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get("/", response_model=List[ForexNewsResponse])
async def get_forex_news_list(
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(100, ge=1, le=1000, description="Number of records to return"),
    currency: Optional[str] = Query(None, description="Filter by currency"),
    impact_level: Optional[str] = Query(None, description="Filter by impact level"),
    db: AsyncSession = Depends(get_database),
    forex_service: ForexService = Depends(get_forex_service)
):
    """Get forex news with optional filtering."""
    try:
        filters = {}
        if currency:
            filters["currency"] = currency
        if impact_level:
            filters["impact_level"] = impact_level

        news_list = await forex_service.get_all(db, skip=skip, limit=limit, filters=filters)
        return [ForexNewsResponse.model_validate(news) for news in news_list]
    except DatabaseError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get("/by-date/{news_date}", response_model=List[ForexNewsResponse])
async def get_forex_news_by_date(
    news_date: date,
    db: AsyncSession = Depends(get_database),
    forex_service: ForexService = Depends(get_forex_service)
):
    """Get forex news by date."""
    try:
        news_list = await forex_service.get_news_by_date(db, news_date)
        return [ForexNewsResponse.model_validate(news) for news in news_list]
    except DatabaseError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get("/by-currency/{currency}", response_model=List[ForexNewsResponse])
async def get_forex_news_by_currency(
    currency: str,
    db: AsyncSession = Depends(get_database),
    forex_service: ForexService = Depends(get_forex_service)
):
    """Get forex news by currency."""
    try:
        news_list = await forex_service.get_news_by_currency(db, currency)
        return [ForexNewsResponse.model_validate(news) for news in news_list]
    except DatabaseError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get("/by-impact/{impact_level}", response_model=List[ForexNewsResponse])
async def get_forex_news_by_impact_level(
    impact_level: str,
    db: AsyncSession = Depends(get_database),
    forex_service: ForexService = Depends(get_forex_service)
):
    """Get forex news by impact level."""
    try:
        news_list = await forex_service.get_news_by_impact_level(db, impact_level)
        return [ForexNewsResponse.model_validate(news) for news in news_list]
    except DatabaseError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))




@router.get("/search/", response_model=List[ForexNewsResponse])
async def search_forex_news(
    query: str = Query(..., description="Search query"),
    currency: Optional[str] = Query(None, description="Filter by currency"),
    impact_level: Optional[str] = Query(None, description="Filter by impact level"),
    db: AsyncSession = Depends(get_database),
    forex_service: ForexService = Depends(get_forex_service)
):
    """Search forex news."""
    try:
        news_list = await forex_service.search_news(db, query, currency, impact_level)
        return [ForexNewsResponse.model_validate(news) for news in news_list]
    except DatabaseError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get("/statistics/", response_model=dict)
async def get_forex_news_statistics(
    db: AsyncSession = Depends(get_database),
    forex_service: ForexService = Depends(get_forex_service)
):
    """Get forex news statistics."""
    try:
        stats = await forex_service.get_news_statistics(db)
        return stats
    except DatabaseError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


# Additional endpoints expected by tests
@router.get("/by-date-range/", response_model=List[ForexNewsResponse])
async def get_forex_news_by_date_range(
    start_date: date = Query(..., description="Start date"),
    end_date: date = Query(..., description="End date"),
    db: AsyncSession = Depends(get_database),
    forex_service: ForexService = Depends(get_forex_service)
):
    """Get forex news by date range."""
    try:
        news_list = await forex_service.get_forex_news_by_date_range(db, start_date, end_date)
        return [ForexNewsResponse.model_validate(news) for news in news_list]
    except DatabaseError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get("/today/", response_model=List[ForexNewsResponse])
async def get_todays_forex_news(
    db: AsyncSession = Depends(get_database),
    forex_service: ForexService = Depends(get_forex_service)
):
    """Get today's forex news."""
    try:
        news_list = await forex_service.get_todays_forex_news(db)
        return [ForexNewsResponse.model_validate(news) for news in news_list]
    except DatabaseError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get("/filtered/", response_model=List[ForexNewsResponse])
async def get_forex_news_with_filters(
    currency: Optional[str] = Query(None, description="Filter by currency"),
    impact_level: Optional[str] = Query(None, description="Filter by impact level"),
    db: AsyncSession = Depends(get_database),
    forex_service: ForexService = Depends(get_forex_service)
):
    """Get forex news with filters."""
    try:
        filters = {}
        if currency:
            filters["currency"] = currency
        if impact_level:
            filters["impact_level"] = impact_level

        news_list = await forex_service.get_forex_news_with_filters(db, filters)
        return [ForexNewsResponse.model_validate(news) for news in news_list]
    except DatabaseError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.post("/bulk/", response_model=List[ForexNewsResponse], status_code=status.HTTP_201_CREATED)
async def bulk_create_forex_news(
    news_data_list: List[ForexNewsCreate],
    db: AsyncSession = Depends(get_database),
    forex_service: ForexService = Depends(get_forex_service)
):
    """Bulk create forex news."""
    try:
        created_news = await forex_service.bulk_create(db, news_data_list)
        return [ForexNewsResponse.model_validate(news) for news in created_news]
    except ValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except DatabaseError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.delete("/{news_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_forex_news(
    news_id: int,
    db: AsyncSession = Depends(get_database),
    forex_service: ForexService = Depends(get_forex_service)
):
    """Delete forex news."""
    try:
        success = await forex_service.delete(db, news_id)
        if not success:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Forex news not found")
        # Return 204 No Content - no response body
    except DatabaseError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
