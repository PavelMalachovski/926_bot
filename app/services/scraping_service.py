"""Advanced web scraping service with human-like behavior and Cloudflare bypass."""

import asyncio
import logging
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime
from typing import Optional, List, Dict, Any

import undetected_chromedriver as uc
from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
import structlog

from app.core.config import settings
from app.core.exceptions import ScrapingError, ExternalAPIError
from app.models.forex_news import ForexNewsCreate
from .scraping_selenium import SeleniumScraper
from .scraping_parser import ForexNewsParser

logger = structlog.get_logger(__name__)


class CloudflareBypassError(Exception):
    """Custom exception for Cloudflare challenge detection."""
    pass


class ChatGPTAnalyzer:
    """Handles ChatGPT API integration for news analysis."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or settings.api.openai_api_key
        self.api_url = "https://api.openai.com/v1/chat/completions"
        if not self.api_key:
            logger.warning("ChatGPT API key not configured. Analysis will be skipped.")

    def analyze_news(self, news_item: Dict[str, str]) -> str:
        """Analyze forex news using ChatGPT API."""
        if not self.api_key:
            return "⚠️ ChatGPT analysis skipped: API key not configured."

        try:
            import requests
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            prompt = self._create_analysis_prompt(news_item)
            data = {
                "model": "gpt-4",
                "messages": [
                    {"role": "system", "content": "You are a financial analyst specializing in Forex markets."},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 150,
                "temperature": 0.7,
            }
            response = requests.post(self.api_url, headers=headers, json=data, timeout=10)
            response.raise_for_status()
            result = response.json()
            analysis = result["choices"][0]["message"]["content"].strip()
            return self._escape_markdown_v2(analysis)
        except Exception as e:
            logger.error("ChatGPT analysis failed", error=str(e))
            return "⚠️ Error in ChatGPT analysis."

    def _create_analysis_prompt(self, news_item: Dict[str, str]) -> str:
        """Create analysis prompt for ChatGPT."""
        return (
            f"Analyze the following Forex news and predict its potential market impact:\n"
            f"Time: {news_item['time']}\n"
            f"Currency: {news_item['currency']}\n"
            f"Event: {news_item['event']}\n"
            f"Actual: {news_item.get('actual', 'N/A')}\n"
            f"Forecast: {news_item['forecast']}\n"
            f"Previous: {news_item['previous']}\n"
            "Provide a concise analysis (up to 100 words) of how this news might affect the market."
        )

    def _escape_markdown_v2(self, text: str) -> str:
        """Escape special characters for Telegram MarkdownV2."""
        escape_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
        for char in escape_chars:
            text = text.replace(char, f'\\{char}')
        return text


class ScrapingService:
    """Advanced web scraping service with human-like behavior and Cloudflare bypass."""

    # Centralized impact class mapping (support both '=' and '-')
    IMPACT_CLASS_MAP = {
        'icon--ff-impact=red': 'high',    # Red (equals)
        'icon--ff-impact=ora': 'medium', # Orange (equals)
        'icon--ff-impact=yel': 'low',    # Yellow (equals)
        'icon--ff-impact-red': 'high',   # Red (dash, fallback)
        'icon--ff-impact-ora': 'medium', # Orange (dash, fallback)
        'icon--ff-impact-yel': 'low',    # Yellow (dash, fallback)
    }

    def __init__(self):
        self.base_url = "https://www.forexfactory.com/calendar"
        self.analyzer = ChatGPTAnalyzer()
        self.session = None
        self.rate_limit_delay = getattr(settings.chart, 'yf_min_request_interval_sec', 1)
        self.selenium_scraper = SeleniumScraper()
        self.parser = ForexNewsParser()

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        pass

    async def scrape_forex_calendar(self, date: datetime) -> List[ForexNewsCreate]:
        """Scrape forex calendar for a specific date."""
        try:
            logger.info("Scraping forex calendar", date=date)

            # Use the sophisticated scraping logic
            news_items = await self.scrape_news(date, analysis_required=False)

            # Convert to ForexNewsCreate objects
            forex_news_list = []
            for item in news_items:
                # Create datetime for the event
                try:
                    event_time = datetime.combine(date.date(), datetime.strptime(item["time"], "%H:%M").time())
                except ValueError:
                    # Handle different time formats
                    event_time = date

                news = ForexNewsCreate(
                    date=event_time,
                    time=item["time"],
                    currency=item["currency"],
                    event=item["event"],
                    forecast=item.get("forecast"),
                    previous=item.get("previous"),
                    actual=item.get("actual"),
                    impact_level=item.get("impact", "unknown"),
                    analysis=item.get("analysis")
                )
                forex_news_list.append(news)

            logger.info("Scraped forex news", count=len(forex_news_list))
            return forex_news_list

        except Exception as e:
            logger.error("Failed to scrape forex calendar", date=date, error=str(e), exc_info=True)
            raise ScrapingError(f"Failed to scrape forex calendar: {e}")

    async def scrape_news(self, target_date: Optional[datetime] = None, analysis_required: bool = False, debug: bool = False) -> List[Dict[str, Any]]:
        """Scrape forex news from ForexFactory using undetected-chromedriver."""
        if target_date is None:
            target_date = datetime.now()

        url = self._build_url(target_date)
        logger.info("Fetching URL", url=url)

        # Try the new Selenium approach with Cloudflare challenge handling
        try:
            html = await self.selenium_scraper.scrape_with_selenium(url)
            logger.info("Successfully scraped with Selenium")
        except Exception as e:
            logger.error("Selenium scraping failed", error=str(e))
            # Fallback to the old method if Selenium fails
            try:
                logger.info("Trying fallback method...")
                html = await asyncio.to_thread(self.selenium_scraper.fetch_with_undetected_chromedriver, url)
                logger.info("Successfully scraped with fallback method")
            except Exception as fallback_e:
                logger.error("Fallback method also failed", error=str(fallback_e))
                raise CloudflareBypassError(f"All scraping methods failed: {e}, fallback: {fallback_e}")

        news_items = self._parse_news_from_html(html)

        # Disable ChatGPT analysis globally for now
        analysis_required = False
        if analysis_required:
            # Group by (currency, time)
            grouped = {}
            for item in news_items:
                key = (item['currency'], item['time'])
                grouped.setdefault(key, []).append(item)
            for group_items in grouped.values():
                if len(group_items) > 1:
                    # Group event: create a single group analysis
                    group_prompt = {
                        'time': group_items[0]['time'],
                        'currency': group_items[0]['currency'],
                        'event': ", ".join([i['event'] for i in group_items]),
                        'actual': ", ".join([i['actual'] for i in group_items]),
                        'forecast': ", ".join([i['forecast'] for i in group_items]),
                        'previous': ", ".join([i['previous'] for i in group_items]),
                    }
                    group_analysis = self.analyzer.analyze_news(group_prompt)
                    for i in group_items:
                        i['analysis'] = group_analysis
                        i['group_analysis'] = True
                else:
                    group_items[0]['analysis'] = self.analyzer.analyze_news(group_items[0])
                    group_items[0]['group_analysis'] = False
        else:
            for item in news_items:
                item['analysis'] = None
                item['group_analysis'] = False

        logger.info("Collected news items", count=len(news_items))
        return news_items

    def _build_url(self, target_date: datetime) -> str:
        """Build ForexFactory URL for the target date."""
        date_str = target_date.strftime("%b%d.%Y").lower()
        return f"{self.base_url}?day={date_str}"

    def _parse_news_from_html(self, html: str) -> List[Dict[str, str]]:
        """Parse forex news from HTML content."""
        soup = BeautifulSoup(html, 'html.parser')

        # Cloudflare/fallback detection
        if "cloudflare" in html.lower() or "just a moment" in html.lower() or "attention required" in html.lower():
            logger.warning("Cloudflare or fallback content detected in page source!")

        # Try multiple selectors for event rows
        selectors = [
            'table.calendar__table tr.calendar__row[data-event-id]',
            'table.calendar__table tr.calendar__row',
            'tr.calendar__row[data-event-id]',
            'tr.calendar__row',
            'table.calendar tr.event',
            'tr.event',
        ]

        rows = []
        for selector in selectors:
            rows = soup.select(selector)
            if rows:
                logger.info("Found rows with selector", count=len(rows), selector=selector)
                break

        logger.debug("Found rows to process for impact extraction", count=len(rows))

        if not rows:
            logger.warning("No news rows found with any selector. Saving HTML to /tmp/forex_debug.html for inspection.")
            try:
                with open("/tmp/forex_debug.html", "w", encoding="utf-8") as f:
                    f.write(html)
            except Exception as e:
                logger.warning("Failed to save debug HTML", error=str(e))

        # First pass: collect all news items and track times
        news_items: List[Dict[str, str]] = []
        current_time = "N/A"
        all_classes = []

        for row in rows:
            logger.debug("EXTRACTING NEWS DATA for row", row=str(row))
            # Collect all impact classes for debugging
            impact_element = (
                row.select_one('.calendar__impact span.icon')
                or row.select_one('.impact span.icon')
            )
            if impact_element:
                classes = impact_element.get('class', [])
                all_classes.append(classes)

            news_item = self.parser.extract_news_data(row)
            if news_item["time"] != "N/A" and news_item["time"].strip():
                current_time = news_item["time"]
            elif current_time != "N/A":
                news_item["time"] = current_time

            news_items.append(news_item)

        if not news_items:
            logger.warning("No news items collected", impact_classes=all_classes)

        # Second pass: ensure all items have times and sort properly
        news_items = self.parser.ensure_all_times_and_sort(news_items)
        return news_items

    # Legacy methods for compatibility
    async def scrape_yahoo_finance_news(self, symbol: str) -> List[Dict[str, Any]]:
        """Scrape news from Yahoo Finance for a specific symbol."""
        logger.info("Yahoo Finance scraping not implemented", symbol=symbol)
        return []

    async def scrape_investing_com_calendar(self, date: datetime) -> List[Dict[str, Any]]:
        """Scrape forex calendar from investing.com."""
        logger.info("Investing.com scraping not implemented", date=date)
        return []

    async def scrape_forex_factory_calendar(self, date: datetime) -> List[Dict[str, Any]]:
        """Scrape forex calendar from forex-factory.com."""
        # This is now handled by the main scrape_news method
        return await self.scrape_news(date)

    async def get_market_sentiment(self, currency: str) -> Dict[str, Any]:
        """Get market sentiment for a currency."""
        logger.info("Market sentiment not implemented", currency=currency)
        return {
            "currency": currency,
            "sentiment": "neutral",
            "confidence": 0.5,
            "sources": ["news"],
            "timestamp": datetime.utcnow().isoformat()
        }

    async def scrape_crypto_news(self) -> List[Dict[str, Any]]:
        """Scrape cryptocurrency news."""
        logger.info("Crypto news scraping not implemented")
        return []

    async def scrape_central_bank_announcements(self) -> List[Dict[str, Any]]:
        """Scrape central bank announcements."""
        logger.info("Central bank announcements scraping not implemented")
        return []
