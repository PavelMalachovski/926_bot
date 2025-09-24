"""Chart API endpoints."""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from io import BytesIO

from app.database.connection import get_database
from app.services.chart_service import ChartService
from app.models.chart import ChartRequest, ChartResponse
from app.core.exceptions import ChartGenerationError, ValidationError

router = APIRouter()


def get_chart_service() -> ChartService:
    """Get chart service instance."""
    return ChartService()


@router.post("/generate", response_model=ChartResponse)
async def generate_chart(
    chart_request: ChartRequest,
    chart_service: ChartService = Depends(get_chart_service)
):
    """Generate a chart for the given request."""
    try:
        response = await chart_service.generate_chart(chart_request)
        return response
    except ValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except ChartGenerationError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.post("/generate/image")
async def generate_chart_image(
    chart_request: ChartRequest,
    chart_service: ChartService = Depends(get_chart_service)
):
    """Generate a chart image and return it as a streaming response."""
    try:
        response = await chart_service.generate_chart(chart_request)

        if not response.success or not response.chart_image:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=response.error_message or "Chart generation failed"
            )

        # Return the image as a streaming response
        return StreamingResponse(
            BytesIO(response.chart_image),
            media_type="image/png",
            headers={
                "Content-Disposition": f"attachment; filename=chart_{chart_request.currency}_{chart_request.event_name.replace(' ', '_')}.png"
            }
        )

    except ValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except ChartGenerationError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get("/currencies/")
async def get_supported_currencies():
    """Get list of supported currencies for chart generation."""
    return {
        "currencies": [
            "USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD",
            "CNY", "INR", "BRL", "RUB", "KRW", "MXN", "SGD", "HKD",
            "XAU", "BTC", "ETH"
        ],
        "impact_levels": ["high", "medium", "low"],
        "chart_types": ["single", "multi"],
        "window_hours_range": {"min": 1, "max": 24}
    }


@router.get("/health")
async def chart_service_health():
    """Check chart service health."""
    return {
        "status": "healthy",
        "service": "chart_service",
        "supported_features": [
            "candlestick_charts",
            "volume_analysis",
            "event_annotations",
            "price_change_analysis"
        ]
    }
