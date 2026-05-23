"""
czbooks.net scraper.

Strategy:
  - Index page + chapter pages are static HTML on czbooks.net.
  - Default fast path: requests + ThreadPoolExecutor.
  - Playwright is kept as a fallback for any chapter where requests returns
    empty content (covers future JS-rendered pages without breaking happy path).
"""
import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    cover_url: str = ""
    chapters: list = field(default_factory=list)  # list[Chapter]


# ---------------------------------------------------------------------------
# HTML parsing helpers
# ---------------------------------------------------------------------------

def _normalize_url(href: str) -> str:
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    return BASE_URL + href


def _parse_index_html(html: str, url: str) -> Optional[Novel]:
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    h1 = soup.select_one("h1.title, .book-title h1, h1")
    if h1:
        title = h1.get_text(strip=True)
    if not title:
        page_title = soup.title.get_text(strip=True) if soup.title else ""
        m = re.search(r"《(.+?)》", page_title)
        if m:
            title = m.group(1)

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

    cover_url = ""
    cover_img = soup.select_one(".thumbnail img, .novel-detail .thumbnail img, .book-cover img")
    if cover_img:
        src = cover_img.get("src", "")
        if src and "default_no_thumbnail" not in src:
            cover_url = _normalize_url(src)

    logger.info(f"Found novel: 《{title}》 by {author}, {len(chapters)} chapters, cover={'yes' if cover_url else 'no'}")
    return Novel(title=title, author=author, url=url, cover_url=cover_url, chapters=chapters)


_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


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
    if not content_tag:
        logger.warning(f"No content selector matched: {url}")
        return ""

    # czbooks chapter pages use <br>-separated text in .content; the only <p>
    # tags are decorative (MV link). Prefer <br>-splitting whenever <br> tags
    # exist in the container so we don't miss the body.
    if content_tag.find("br"):
        # Tree-mutation via br.replace_with("\n") breaks subsequent siblings
        # under html.parser, so do the substitution at string level.
        inner = _BR_RE.sub("\n", content_tag.decode_contents())
        text = BeautifulSoup(inner, "html.parser").get_text("\n")
        lines = [ln.strip() for ln in text.splitlines()]
        lines = [ln for ln in lines if ln]
        if lines:
            return "\n\n".join(lines)

    # Pure <p>-based pages
    paras = [p.get_text(strip=True) for p in content_tag.find_all("p")]
    paras = [p for p in paras if p]
    if paras:
        return "\n\n".join(paras)

    return content_tag.get_text(separator="\n", strip=True)


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------

class Scraper:
    """
    czbooks scraper with requests-first fast path and Playwright fallback.

    Usage:
        scraper = Scraper(concurrency=10)
        novel = scraper.parse_novel_index(url)
        scraper.fetch_chapters(novel, start, end, callback)
    """

    def __init__(self, concurrency: int = 10, page_timeout: int = 20000, request_timeout: int = 15):
        self.concurrency = concurrency
        self.page_timeout = page_timeout  # Playwright (ms)
        self.request_timeout = request_timeout  # requests (s)
        self._session = requests.Session()
        self._session.headers.update(HEADERS)

    # ------------------------------------------------------------------
    # Index page
    # ------------------------------------------------------------------

    def _fetch_with_playwright_sync(self, url: str) -> Optional[str]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.warning("Playwright not installed; index fallback unavailable")
            return None
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
        try:
            resp = self._session.get(url, timeout=self.request_timeout)
            if resp.status_code == 200:
                novel = _parse_index_html(resp.text, url)
                if novel and novel.chapters:
                    return novel
        except Exception as e:
            logger.debug(f"requests index failed: {e}")

        logger.info("Fetching index via playwright...")
        html = self._fetch_with_playwright_sync(url)
        if not html:
            return None
        return _parse_index_html(html, url)

    def dump_raw_html(self, url: str) -> str:
        try:
            resp = self._session.get(url, timeout=self.request_timeout)
            if resp.status_code == 200:
                return resp.text
        except Exception:
            pass
        return self._fetch_with_playwright_sync(url) or ""

    # ------------------------------------------------------------------
    # Chapters — requests fast path
    # ------------------------------------------------------------------

    def _fetch_chapter_requests(self, chapter: Chapter) -> bool:
        """Returns True if content was extracted."""
        try:
            resp = self._session.get(chapter.url, timeout=self.request_timeout)
            if resp.status_code != 200:
                return False
            content = _extract_content(resp.text, chapter.url)
            if content:
                chapter.content = content
                return True
        except Exception as e:
            logger.debug(f"requests chapter failed ({chapter.title}): {e}")
        return False

    def fetch_chapters(
        self,
        novel: Novel,
        start: int = 1,
        end: Optional[int] = None,
        progress_callback: Optional[Callable] = None,
    ) -> None:
        """
        Fetch chapter content via requests + ThreadPoolExecutor.
        Chapters that come back empty are retried via Playwright fallback.
        """
        if end is None:
            end = len(novel.chapters)
        subset = novel.chapters[start - 1:end]
        total = len(subset)
        if total == 0:
            return

        done_count = 0
        failed: list = []

        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            future_to_ch = {pool.submit(self._fetch_chapter_requests, ch): ch for ch in subset}
            for future in as_completed(future_to_ch):
                ch = future_to_ch[future]
                ok = False
                try:
                    ok = future.result()
                except Exception as e:
                    logger.warning(f"chapter task crashed ({ch.title}): {e}")
                if not ok:
                    failed.append(ch)
                done_count += 1
                if progress_callback:
                    progress_callback(done_count, total, ch.title)

        if failed:
            logger.info(f"{len(failed)} chapters had empty content; retrying via Playwright...")
            try:
                asyncio.run(self._fetch_all_async(failed, progress_callback=None))
            except Exception as e:
                logger.error(f"Playwright fallback failed: {e}")

    # ------------------------------------------------------------------
    # Chapters — Playwright fallback (rarely used)
    # ------------------------------------------------------------------

    async def _fetch_chapter_async(
        self,
        chapter: Chapter,
        context,
        semaphore: asyncio.Semaphore,
    ) -> None:
        async with semaphore:
            try:
                page = await context.new_page()
                await page.goto(chapter.url, wait_until="domcontentloaded", timeout=self.page_timeout)
                html = await page.content()
                await page.close()
                chapter.content = _extract_content(html, chapter.url)
            except Exception as e:
                logger.warning(f"Playwright chapter failed ({chapter.title}): {e}")
                chapter.content = ""

    async def _fetch_all_async(
        self,
        chapters: list,
        progress_callback: Optional[Callable] = None,
    ) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.error("Playwright not installed; cannot run fallback")
            return

        semaphore = asyncio.Semaphore(min(self.concurrency, 5))
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=HEADERS["User-Agent"],
                locale="zh-TW",
            )
            tasks = [self._fetch_chapter_async(ch, context, semaphore) for ch in chapters]
            await asyncio.gather(*tasks)
            await browser.close()
