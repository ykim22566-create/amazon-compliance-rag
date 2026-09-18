"""Selenium-based fetcher for Amazon Seller Central help pages.

Why Selenium and not requests:
    Seller Central help pages are a SPA. Direct curl on the URL returns
    a ~167 KB React shell with zero help-content text — the actual article
    is JS-injected after window.onload. Empirically verified on
    GMUTB89XM7AATPR3 etc. (no "3.5%" / "surcharge" tokens in raw HTTP body).

Wait strategy:
    The article is rendered into an <article> element. We wait until it
    contains a non-trivial amount of text rather than just for the element
    to exist (the empty stub appears almost instantly).
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

logger = logging.getLogger(__name__)


@dataclass
class FetchResult:
    url: str
    fetched_at: datetime
    html: str
    title: str
    article_text_length: int


class HelpFetcher:
    """Render Amazon help pages with a real browser and return their HTML.

    Reuses one Chrome process across many fetches; call close() when done
    or use as a context manager.
    """

    ARTICLE_SELECTOR = "div.help-content, div#help-content, article"
    MIN_ARTICLE_CHARS = 200
    LOGIN_TITLE_TOKENS = ("Sign In", "Login", "登录")
    ROBOT_TITLE_TOKEN = "Robot Check"

    def __init__(
        self,
        headless: bool = True,
        page_load_timeout: int = 30,
        wait_timeout: int = 20,
        polite_delay_range: tuple[float, float] = (1.5, 3.0),
    ):
        self._headless = headless
        self._page_load_timeout = page_load_timeout
        self._wait_timeout = wait_timeout
        self._polite_delay_range = polite_delay_range
        self._driver: webdriver.Chrome | None = None

    def _ensure_driver(self) -> webdriver.Chrome:
        if self._driver is not None:
            return self._driver
        opts = Options()
        if self._headless:
            opts.add_argument("--headless=new")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--window-size=1280,1800")
        # Force English content. Amazon serves localized help pages based on
        # browser language; without this we get CN/JP/etc. depending on the
        # OS locale, which breaks downstream evaluation (the legacy library
        # and the 15-question test set use the English source of truth).
        opts.add_argument("--lang=en-US")
        opts.add_experimental_option(
            "prefs", {"intl.accept_languages": "en-US,en"}
        )
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=opts)
        driver.set_page_load_timeout(self._page_load_timeout)
        self._driver = driver
        logger.info("Selenium Chrome driver started (headless=%s)", self._headless)
        return driver

    def fetch(self, url: str) -> FetchResult:
        driver = self._ensure_driver()
        driver.get(url)
        title = driver.title or ""
        if self.ROBOT_TITLE_TOKEN in title:
            logger.warning("Robot Check at %s — pausing for manual solve", url)
            input(f"⚠️ Solve the captcha in the visible Chrome window for {url}, then press Enter ...")
            title = driver.title or ""
        if any(token in title for token in self.LOGIN_TITLE_TOKENS):
            raise RuntimeError(f"URL redirected to login page: {url}")

        try:
            WebDriverWait(driver, self._wait_timeout).until(
                lambda d: any(
                    len((el.text or "").strip()) >= self.MIN_ARTICLE_CHARS
                    for el in d.find_elements(By.CSS_SELECTOR, self.ARTICLE_SELECTOR)
                )
            )
        except TimeoutException as exc:
            raise RuntimeError(
                f"Timed out waiting for article content at {url} "
                f"(>= {self.MIN_ARTICLE_CHARS} chars)"
            ) from exc

        html = driver.page_source
        article_text = ""
        for el in driver.find_elements(By.CSS_SELECTOR, self.ARTICLE_SELECTOR):
            text = (el.text or "").strip()
            if len(text) > len(article_text):
                article_text = text

        # polite delay between consecutive fetches
        lo, hi = self._polite_delay_range
        time.sleep(random.uniform(lo, hi))

        return FetchResult(
            url=url,
            fetched_at=datetime.now(timezone.utc),
            html=html,
            title=title,
            article_text_length=len(article_text),
        )

    def close(self) -> None:
        if self._driver is not None:
            try:
                self._driver.quit()
            finally:
                self._driver = None

    def __enter__(self) -> "HelpFetcher":
        self._ensure_driver()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
