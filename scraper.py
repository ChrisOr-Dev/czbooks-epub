"""
czbooks.net scraper — async playwright with persistent browser + concurrent pages.
"""
import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8",
    "Referer": "https://czbooks.net/",
}

BASE_URL = "https://czbooks.net"


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
    chapters: list = field(default_factory=list)  # list[Chapter]


# ---------------------------------------------------------------------------
# HTML parsing helpers (shared between sync index fetch + async chapter fetch)
# ---------------------------------------------------------------------------

def _normalize_url(href: str) -> str:
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    return BASE_URL + href


def _parse_index_html(html: str, url: str) -> Optional[Novel]:
    soup = BeautifulSoup(html, "html.parser")

    # title
    title = ""
    h1 = soup.select_one("h1.title, .book-title h1, h1")
    if h1:
        title = h1.get_text(strip=True)
    if not title:
        page_title = soup.title.get_text(strip=True) if soup.title else ""
        m = re.search(r"《(.+?)》", page_title)
        if m:
            title = m.group(1)

    # author
    author = "Unknown"
    author_tag = soup.select_one(".author a, .book-author a, span.author")
    if author_tag:
        author = re.sub(r"^作者[：:]\s*", "", author_tag.get_text(strip=True))
    if author == "Unknown":
        for text in soup.find_all(string=lambda t: t and "作者" in t):
            m = re.search(r"作者[：:]\s*(.+)", text.strip())
            if m:
                author = m.group(1).strip()
                break

    # chapters
    chapter_links = soup.select(".chapter-list a, ul.chapter a, .chapterList a, #chapter-list a")
    if not chapter_links:
        chapter_links = soup.select("a[href*='/n/']")

    seen: set = set()
    chapters = []
    for a in chapter_links:
        href = a.get("href", "")
        if not href or href in seen or href.rstrip("/") == url.rstrip("/"):
            continue
        seen.add(href)
        chapters.append(Chapter(title=a.get_text(strip=True), url=_normalize_url(href)))

    logger.info(f"Found novel: 《{title}》 by {author}, {len(chapters)} chapters")
    return Novel(title=title, author=author, url=url, chapters=chapters)


def _extract_content(html: str, url: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.select("script,style,.ad,.advertisement,nav,.chapter-nav,.pagination,header,footer"):
        tag.decompose()

    content_tag = (
        soup.select_one(".chapter-content")
        or soup.select_one("#chapter-content")
        or soup.select_one(".content")
        or soup.select_one("article")
        or soup.select_one(".novel-content")
    )
    if content_tag:
        # Try <p> tags first
        paras = [p.get_text(strip=True) for p in content_tag.find_all("p")]
        if paras:
            return "\n\n".join(p for p in paras if p)
        # Fallback: split by <br> tags
        for br in content_tag.find_all("br"):
            br.replace_with("\n")
        lines = [ln.strip() for ln in content_tag.get_text(separator="\n").splitlines()]
        lines = [ln for ln in lines if ln]
        if lines:
            return "\n\n".join(lines)
        return content_tag.get_text(separator="\n", strip=True)

    logger.warning(f"No content selector matched: {url}")
    return ""


# ---------------------------------------------------------------------------
# Async scraper — persistent browser, concurrent pages
# ---------------------------------------------------------------------------

class Scraper:
    """
    Async playwright scraper.

    Usage:
        scraper = Scraper(concurrency=5)
        novel = scraper.parse_novel_index(url)   # sync, playwright one-shot
        scraper.fetch_chapters(novel, start, end, callback)  # async internally
    """

    def __init__(self, concurrency: int = 5, page_timeout: int = 20000):
        self.concurrency = concurrency
        self.page_timeout = page_timeout  # ms
        # requests session for index page (playwright fallback)
        self._session = requests.Session()
        self._session.headers.update(HEADERS)

    # ------------------------------------------------------------------
    # Sync: index page (single fetch, playwright if needed)
    # ------------------------------------------------------------------

    def _fetch_with_playwright_sync(self, url: str) -> Optional[str]:
        from playwright.sync_api import sync_playwright
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                ctx = browser.new_context(user_agent=HEADERS["User-Agent"], locale="zh-TW")
                page = ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1500)
                html = page.content()
                browser.close()
                return html
        except Exception as e:
            logger.error(f"Playwright (sync) error: {e}")
            return None

    def parse_novel_index(self, url: str) -> Optional[Novel]:
        # Try requests first (unlikely to work but fast check)
        try:
            resp = self._session.get(url, timeout=10)
            if resp.status_code == 200:
                return _parse_index_html(resp.text, url)
        except Exception:
            pass

        logger.info("Fetching index via playwright...")
        html = self._fetch_with_playwright_sync(url)
        if not html:
            return None
        return _parse_index_html(html, url)

    def dump_raw_html(self, url: str) -> str:
        try:
            resp = self._session.get(url, timeout=10)
            if resp.status_code == 200:
                return resp.text
        except Exception:
            pass
        return self._fetch_with_playwright_sync(url) or ""

    # ------------------------------------------------------------------
    # Async: chapters — persistent browser, concurrent pages
    # ------------------------------------------------------------------

    async def _fetch_chapter_async(
        self,
        chapter: Chapter,
        context,  # playwright BrowserContext
        semaphore: asyncio.Semaphore,
        index: int,
        total: int,
        progress_callback: Optional[Callable],
        delay: float,
    ) -> None:
        async with semaphore:
            # small random delay to avoid hammering the server
            await asyncio.sleep(random.uniform(0, delay))
            for attempt in range(2):  # retry once on empty content
                try:
                    page = await context.new_page()
                    await page.goto(chapter.url, wait_until="load", timeout=self.page_timeout)
                    await page.wait_for_timeout(800 + attempt * 1200)
                    html = await page.content()
                    await page.close()
                    chapter.content = _extract_content(html, chapter.url)
                    if chapter.content:
                        break
                    logger.debug(f"Empty content on attempt {attempt+1}, retrying: {chapter.title}")
                except Exception as e:
                    logger.warning(f"Attempt {attempt+1} failed for {chapter.title}: {e}")
                    chapter.content = ""
            if progress_callback:
                progress_callback(index, total, chapter.title)

    async def _fetch_all_async(
        self,
        chapters: list,
        progress_callback: Optional[Callable],
    ) -> None:
        from playwright.async_api import async_playwright

        delay = 0.5  # base jitter delay per worker
        semaphore = asyncio.Semaphore(self.concurrency)

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=HEADERS["User-Agent"],
                locale="zh-TW",
            )
            total = len(chapters)
            tasks = [
                self._fetch_chapter_async(ch, context, semaphore, i + 1, total, progress_callback, delay)
                for i, ch in enumerate(chapters)
            ]
            await asyncio.gather(*tasks)
            await browser.close()

    def fetch_chapters(
        self,
        novel: Novel,
        start: int = 1,
        end: Optional[int] = None,
        progress_callback: Optional[Callable] = None,
    ) -> None:
        if end is None:
            end = len(novel.chapters)
        subset = novel.chapters[start - 1 : end]
        asyncio.run(self._fetch_all_async(subset, progress_callback))
