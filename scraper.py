"""
czbooks.net scraper — handles 403 bypass via browser headers or playwright fallback.
"""
import time
import random
import logging
from dataclasses import dataclass, field
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


@dataclass
class Chapter:
    title: str
    url: str
    content: str = ""


@dataclass
class Novel:
    title: str
    author: str
    url: str
    chapters: list[Chapter] = field(default_factory=list)


class Scraper:
    def __init__(self, delay_min: float = 0.5, delay_max: float = 1.5, max_retries: int = 3):
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._playwright_browser = None

    def _sleep(self):
        time.sleep(random.uniform(self.delay_min, self.delay_max))

    def _fetch_requests(self, url: str) -> Optional[str]:
        """Fetch page with requests, return HTML or None on failure."""
        self.session.headers["Referer"] = "https://czbooks.net/"
        for attempt in range(self.max_retries):
            try:
                resp = self.session.get(url, timeout=15)
                if resp.status_code == 200:
                    return resp.text
                if resp.status_code == 403:
                    logger.warning(f"403 on {url}, attempt {attempt+1}")
                    if attempt < self.max_retries - 1:
                        self._sleep()
                    continue
                logger.warning(f"HTTP {resp.status_code} on {url}")
            except Exception as e:
                logger.warning(f"Request error on {url}: {e}")
                if attempt < self.max_retries - 1:
                    self._sleep()
        return None

    def _fetch_playwright(self, url: str) -> Optional[str]:
        """Fetch page using playwright headless browser."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.error("playwright not installed. Run: pip install playwright && playwright install chromium")
            return None

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent=HEADERS["User-Agent"],
                    locale="zh-TW",
                )
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                # Wait for content to load
                page.wait_for_timeout(2000)
                html = page.content()
                # Save cookies back to requests session
                cookies = context.cookies()
                for c in cookies:
                    self.session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))
                browser.close()
                return html
        except Exception as e:
            logger.error(f"Playwright error on {url}: {e}")
            return None

    def fetch(self, url: str, force_playwright: bool = False) -> Optional[str]:
        """Fetch URL, fall back to playwright if requests returns 403."""
        if not force_playwright:
            html = self._fetch_requests(url)
            if html:
                return html
            logger.info("Falling back to playwright...")
        return self._fetch_playwright(url)

    def parse_novel_index(self, url: str) -> Optional[Novel]:
        """Parse novel index page, extract title, author, chapter list."""
        html = self.fetch(url)
        if not html:
            return None

        soup = BeautifulSoup(html, "html.parser")

        # --- title ---
        title = ""
        title_tag = soup.select_one("h1.title, .book-title h1, h1")
        if title_tag:
            title = title_tag.get_text(strip=True)

        # --- author ---
        author = "Unknown"
        author_tag = soup.select_one(".author a, .book-author a, span.author")
        if author_tag:
            author = author_tag.get_text(strip=True)

        # --- chapters ---
        chapters = []
        # czbooks uses a chapter list in .chapter-list or similar
        chapter_links = soup.select(".chapter-list a, ul.chapter a, .chapterList a, #chapter-list a")
        if not chapter_links:
            # Broader fallback
            chapter_links = soup.select("a[href*='/n/']")

        seen = set()
        for a in chapter_links:
            href = a.get("href", "")
            if not href or href in seen:
                continue
            # Filter out the book index URL itself
            if href.rstrip("/") == url.rstrip("/"):
                continue
            seen.add(href)
            full_url = href if href.startswith("http") else "https://czbooks.net" + href
            chapters.append(Chapter(title=a.get_text(strip=True), url=full_url))

        logger.info(f"Found novel: 《{title}》 by {author}, {len(chapters)} chapters")
        return Novel(title=title, author=author, url=url, chapters=chapters)

    def parse_chapter_content(self, chapter: Chapter) -> str:
        """Fetch and extract chapter text content."""
        html = self.fetch(chapter.url)
        if not html:
            logger.error(f"Failed to fetch chapter: {chapter.title}")
            return ""

        soup = BeautifulSoup(html, "html.parser")

        # Remove noise elements
        for tag in soup.select("script, style, .ad, .advertisement, nav, .chapter-nav, .pagination, header, footer"):
            tag.decompose()

        # Try known content selectors
        content_tag = (
            soup.select_one(".chapter-content")
            or soup.select_one("#chapter-content")
            or soup.select_one(".content")
            or soup.select_one("article")
            or soup.select_one(".novel-content")
        )

        if content_tag:
            paragraphs = [p.get_text(strip=True) for p in content_tag.find_all("p")]
            if paragraphs:
                return "\n\n".join(p for p in paragraphs if p)
            # Fallback: raw text
            return content_tag.get_text(separator="\n", strip=True)

        logger.warning(f"Could not find content selector for {chapter.url}")
        return ""

    def fetch_chapters(
        self,
        novel: Novel,
        start: int = 1,
        end: Optional[int] = None,
        progress_callback=None,
    ) -> None:
        """Fill chapter content in-place. start/end are 1-indexed."""
        chapters = novel.chapters
        if end is None:
            end = len(chapters)
        subset = chapters[start - 1 : end]
        total = len(subset)

        for i, chapter in enumerate(subset, 1):
            logger.info(f"[{i}/{total}] Fetching: {chapter.title}")
            chapter.content = self.parse_chapter_content(chapter)
            if progress_callback:
                progress_callback(i, total, chapter.title)
            if i < total:
                self._sleep()

    def dump_raw_html(self, url: str) -> str:
        """Return raw HTML for debugging."""
        html = self.fetch(url)
        return html or ""
