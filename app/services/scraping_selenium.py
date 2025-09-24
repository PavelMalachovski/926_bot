"""Selenium-based scraping methods for the scraping service."""

import asyncio
import os
import random
import re
import subprocess
import sys
import time
from typing import Optional, List

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
import structlog

logger = structlog.get_logger(__name__)


class SeleniumScraper:
    """Selenium-based scraper with human-like behavior and Cloudflare bypass."""

    def __init__(self):
        pass

    async def scrape_with_selenium(self, url: str) -> str:
        """Scrape using undetected-chromedriver with human-like behavior."""
        try:
            # Resolve Chrome binary cross-platform
            chrome_binary = self._find_chrome_binary()
            if chrome_binary:
                logger.info("Using Chrome binary", path=chrome_binary)
            else:
                logger.warning("Chrome binary not found via known paths; proceeding without explicit binary_location")

            # Try to detect installed Chrome major version for matching driver
            chrome_major_version = self._get_chrome_major_version(chrome_binary)
            if chrome_major_version:
                logger.info("Detected Chrome major version", version=chrome_major_version)
            else:
                logger.warning("Could not detect Chrome version; letting undetected-chromedriver decide")

            options = uc.ChromeOptions()
            if chrome_binary:
                options.binary_location = chrome_binary
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            options.add_argument("--disable-web-security")
            options.add_argument("--disable-features=VizDisplayCompositor")
            options.add_argument("--disable-extensions")
            options.add_argument("--disable-plugins")
            # Don't disable images and JavaScript for Cloudflare challenge
            options.add_argument("--disable-default-apps")
            options.add_argument("--disable-sync")
            options.add_argument("--disable-translate")
            options.add_argument("--disable-background-timer-throttling")
            options.add_argument("--disable-backgrounding-occluded-windows")
            options.add_argument("--disable-renderer-backgrounding")
            options.add_argument("--disable-field-trial-config")
            options.add_argument("--disable-ipc-flooding-protection")
            options.add_argument("--no-first-run")
            options.add_argument("--no-default-browser-check")
            options.add_argument("--disable-blink-features=AutomationControlled")
            # Add headless mode
            options.add_argument("--headless=new")
            # Add user agent
            options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

            # Pin driver to installed Chrome major version when known to avoid mismatch
            if chrome_major_version:
                driver = uc.Chrome(options=options, version_main=int(chrome_major_version))
            else:
                driver = uc.Chrome(options=options)
            driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

            try:
                logger.info("Navigating to URL", url=url)
                driver.get(url)

                # Wait for page to load and check for Cloudflare challenge
                max_wait = 30
                wait_time = 0
                while wait_time < max_wait:
                    page_source = driver.page_source

                    # Check if we're still on Cloudflare challenge page
                    if "just a moment" in page_source.lower() or "verifying you are human" in page_source.lower():
                        logger.info("Cloudflare challenge detected, waiting...")
                        await asyncio.sleep(2)
                        wait_time += 2
                        continue

                    # Check if we have actual content
                    if "calendar" in page_source.lower() and ("forexfactory" in page_source.lower() or "forex factory" in page_source.lower()):
                        logger.info("Page loaded successfully")
                        break

                    logger.info("Waiting for page to load...")
                    await asyncio.sleep(1)
                    wait_time += 1

                if wait_time >= max_wait:
                    logger.warning("Timeout waiting for page to load")

                # Add human-like behavior
                self._add_human_behavior(driver)

                # Get final page source
                page_source = driver.page_source

                # Final check for Cloudflare challenge
                if "just a moment" in page_source.lower() or "verifying you are human" in page_source.lower():
                    logger.warning("Still on Cloudflare challenge page after waiting")
                    return page_source

                logger.info("Scraping completed successfully")
                return page_source

            finally:
                driver.quit()

        except Exception as e:
            logger.error("Selenium scraping failed", error=str(e))
            raise Exception(f"Selenium error: {e}")

    def _add_human_behavior(self, driver):
        """Add human-like behavior to avoid detection."""
        try:
            # Random mouse movements
            for _ in range(random.randint(2, 5)):
                x = random.randint(100, 800)
                y = random.randint(100, 600)
                action = ActionChains(driver)
                action.move_by_offset(x, y).perform()
                time.sleep(random.uniform(0.1, 0.3))

            # Scroll down and up
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight/2);")
            time.sleep(random.uniform(0.5, 1.0))
            driver.execute_script("window.scrollTo(0, 0);")
            time.sleep(random.uniform(0.3, 0.7))

            # Random key presses
            body = driver.find_element(By.TAG_NAME, "body")
            for _ in range(random.randint(1, 3)):
                body.send_keys(Keys.PAGE_DOWN)
                time.sleep(random.uniform(0.2, 0.5))
                body.send_keys(Keys.PAGE_UP)
                time.sleep(random.uniform(0.2, 0.5))

        except Exception as e:
            logger.warning("Human behavior simulation failed", error=str(e))

    def fetch_with_undetected_chromedriver(self, url: str) -> str:
        """Fallback method using undetected-chromedriver."""
        options = uc.ChromeOptions()
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-infobars")
        options.add_argument("--disable-extensions")
        options.add_argument("--start-maximized")
        options.add_argument("--headless=new")  # Always headless in Docker/cloud
        options.add_argument("--disable-software-rasterizer")
        options.add_argument("--disable-setuid-sandbox")
        options.add_argument("--remote-debugging-port=9222")

        # Resolve Chrome binary from env or OS defaults
        chrome_binary = os.environ.get("CHROME_BINARY") or self._find_chrome_binary()
        if chrome_binary:
            options.binary_location = chrome_binary
            logger.info("Launching Google Chrome with binary", path=options.binary_location)
        else:
            logger.warning("CHROME_BINARY not set and Chrome binary not found via known paths; proceeding without explicit binary_location")

        logger.info("ChromeOptions", arguments=options.arguments)
        logger.info("use_subprocess set to False for compatibility")

        # Match driver version to installed Chrome if known
        chrome_major_version = self._get_chrome_major_version(chrome_binary)
        if chrome_major_version:
            driver = uc.Chrome(options=options, use_subprocess=False, version_main=int(chrome_major_version))
        else:
            driver = uc.Chrome(options=options, use_subprocess=False)

        try:
            driver.get(url)
            actions = ActionChains(driver)
            # Human-like actions: mouse movement, scrolling, key presses, random waits
            for _ in range(random.randint(3, 7)):
                # Move mouse to random positions
                x = random.randint(100, 1200)
                y = random.randint(100, 700)
                actions.move_by_offset(x, y).perform()
                time.sleep(random.uniform(0.2, 0.7))
                actions.move_by_offset(-x, -y).perform()
                time.sleep(random.uniform(0.2, 0.7))
                # Scroll randomly
                scroll_amount = random.randint(100, 800)
                driver.execute_script(f"window.scrollBy(0, {scroll_amount});")
                time.sleep(random.uniform(0.2, 0.7))
                driver.execute_script(f"window.scrollBy(0, {-scroll_amount});")
                time.sleep(random.uniform(0.2, 0.7))
                # Random key press
                if random.random() < 0.5:
                    actions.send_keys(random.choice([Keys.ARROW_DOWN, Keys.ARROW_UP, Keys.PAGE_DOWN, Keys.PAGE_UP])).perform()
                    time.sleep(random.uniform(0.2, 0.7))

            # Click somewhere on the page
            body = driver.find_element(By.TAG_NAME, "body")
            actions.move_to_element(body).click().perform()
            time.sleep(random.uniform(0.5, 1.5))

            # Wait for the calendar table
            for _ in range(60):  # up to 60 seconds
                try:
                    table = driver.find_element(By.CSS_SELECTOR, 'table.calendar__table')
                    if table.is_displayed():
                        break
                except Exception:
                    pass
                time.sleep(1)

            html = driver.page_source
            return html
        finally:
            driver.quit()

    def _find_chrome_binary(self) -> str:
        """Attempt to find the Chrome/Chromium binary across platforms."""
        candidate_paths: List[str] = []
        if sys.platform.startswith("linux"):
            candidate_paths = [
                "/usr/bin/google-chrome-stable",
                "/usr/bin/google-chrome",
                "/usr/bin/chromium-browser",
                "/usr/bin/chromium",
            ]
        elif sys.platform.startswith("win"):
            program_files = os.environ.get("PROGRAMFILES", r"C:\\Program Files")
            program_files_x86 = os.environ.get("PROGRAMFILES(X86)", r"C:\\Program Files (x86)")
            local_app_data = os.environ.get("LOCALAPPDATA", r"C:\\Users\\%USERNAME%\\AppData\\Local")
            candidate_paths = [
                os.path.join(program_files, "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(program_files_x86, "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(local_app_data, "Google", "Chrome", "Application", "chrome.exe"),
            ]
        elif sys.platform == "darwin":
            candidate_paths = [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            ]

        for path in candidate_paths:
            try:
                if os.path.exists(path):
                    return path
            except Exception:
                continue
        return ""

    def _get_chrome_major_version(self, chrome_binary_path: Optional[str]) -> str:
        """Return Chrome major version as string if detectable, else empty string."""
        try:
            version_output = ""
            if chrome_binary_path and os.path.exists(chrome_binary_path):
                # On Windows and Mac/Linux, Chrome supports --version
                proc = subprocess.run([chrome_binary_path, "--version"], capture_output=True, text=True, timeout=5)
                version_output = (proc.stdout or proc.stderr or "").strip()
            else:
                # Try generic 'google-chrome --version' in PATH
                for cmd in ["google-chrome", "google-chrome-stable", "chrome", "chromium", "chromium-browser"]:
                    try:
                        proc = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=3)
                        if proc.returncode == 0 and (proc.stdout or proc.stderr):
                            version_output = (proc.stdout or proc.stderr).strip()
                            if version_output:
                                break
                    except Exception:
                        continue

            if not version_output:
                return ""

            # version_output examples:
            # "Google Chrome 138.0.7204.183" or "Chromium 120.0.0.0"
            m = re.search(r"(\d+)\.\d+\.\d+\.\d+", version_output)
            return m.group(1) if m else ""
        except Exception:
            return ""
