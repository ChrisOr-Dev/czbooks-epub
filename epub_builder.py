"""
EPUB builder — converts Novel dataclass to .epub file using ebooklib.
"""
import logging
import os
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from ebooklib import epub

from scraper import HEADERS, Novel, Chapter

logger = logging.getLogger(__name__)


def _sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name).strip()


def _fetch_cover(url: str) -> Optional[tuple[bytes, str, str]]:
    """Download cover image. Returns (bytes, filename, media_type) or None."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        logger.warning(f"cover fetch failed ({url}): {e}")
        return None
    ext = os.path.splitext(urlparse(url).path)[1].lower().lstrip(".") or "jpg"
    if ext not in ("jpg", "jpeg", "png", "webp", "gif"):
        ext = "jpg"
    media_type = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png", "webp": "image/webp", "gif": "image/gif",
    }[ext]
    return resp.content, f"cover.{ext}", media_type


def _chapter_to_html(chapter: Chapter, index: int) -> str:
    paragraphs = ""
    for line in chapter.content.split("\n"):
        line = line.strip()
        if line:
            paragraphs += f"    <p>{line}</p>\n"
    return f"""<?xml version='1.0' encoding='utf-8'?>
<!DOCTYPE html PUBLIC '-//W3C//DTD XHTML 1.1//EN'
  'http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd'>
<html xmlns='http://www.w3.org/1999/xhtml'>
<head>
  <title>{chapter.title}</title>
  <meta charset='utf-8'/>
</head>
<body>
  <h2>{chapter.title}</h2>
{paragraphs}</body>
</html>"""


def build_epub(
    novel: Novel,
    output_path: Optional[str] = None,
    output_dir: Optional[str] = None,
) -> str:
    """Build EPUB from Novel and save to disk. Returns final file path.

    If output_path is given, it wins. Else builds a filename from the title and
    drops it under output_dir (created if missing).
    """
    book = epub.EpubBook()
    book.set_identifier(f"czbooks-{hash(novel.url) & 0xFFFFFF:06x}")
    book.set_title(novel.title)
    book.set_language("zh")
    book.add_author(novel.author)

    if novel.cover_url:
        cover = _fetch_cover(novel.cover_url)
        if cover:
            data, filename, _media = cover
            book.set_cover(filename, data)
            logger.info(f"cover added: {filename} ({len(data)} bytes)")

    # Style
    style = epub.EpubItem(
        uid="style",
        file_name="style.css",
        media_type="text/css",
        content=b"""
body { font-family: serif; line-height: 1.8; margin: 2em; }
h2 { font-size: 1.4em; margin-bottom: 1em; }
p { text-indent: 2em; margin: 0.5em 0; }
""",
    )
    book.add_item(style)

    epub_chapters = []
    for i, chapter in enumerate(novel.chapters, 1):
        if not chapter.content:
            logger.warning(f"Skipping empty chapter: {chapter.title}")
            continue
        file_name = f"chapter_{i:04d}.xhtml"
        epub_chapter = epub.EpubHtml(
            title=chapter.title,
            file_name=file_name,
            lang="zh",
        )
        epub_chapter.content = _chapter_to_html(chapter, i).encode("utf-8")
        epub_chapter.add_item(style)
        book.add_item(epub_chapter)
        epub_chapters.append(epub_chapter)

    book.toc = tuple(epub.Link(ch.file_name, ch.title, ch.id) for ch in epub_chapters)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + epub_chapters

    if output_path is None:
        safe_name = _sanitize_filename(novel.title) or "novel"
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
            output_path = os.path.join(output_dir, f"{safe_name}.epub")
        else:
            output_path = f"{safe_name}.epub"
    else:
        parent = os.path.dirname(output_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    epub.write_epub(output_path, book)
    logger.info(f"EPUB saved: {output_path}")
    return output_path
