#!/usr/bin/env python3
"""
czbooks.net → EPUB CLI tool

Usage:
  python main.py <url>
  python main.py <url> -o output.epub
  python main.py <url> --chapters 1-50
  python main.py <url> --test           # parse index only
  python main.py <url> --test-chapter 1 # dump first chapter HTML
"""
import argparse
import logging
import sys

from scraper import Scraper
from epub_builder import build_epub


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def parse_chapter_range(spec: str, total: int) -> tuple[int, int]:
    """Parse '1-50' or '10' into (start, end) 1-indexed."""
    if "-" in spec:
        parts = spec.split("-", 1)
        start = int(parts[0]) if parts[0] else 1
        end = int(parts[1]) if parts[1] else total
    else:
        start = end = int(spec)
    start = max(1, start)
    end = min(total, end)
    return start, end


def progress_bar(current: int, total: int, title: str):
    bar_width = 30
    filled = int(bar_width * current / total)
    bar = "█" * filled + "░" * (bar_width - filled)
    pct = current / total * 100
    print(f"\r[{bar}] {pct:5.1f}%  {current}/{total}  {title[:30]}", end="", flush=True)
    if current == total:
        print()


def main():
    parser = argparse.ArgumentParser(
        description="Download czbooks.net novel and convert to EPUB"
    )
    parser.add_argument("url", help="Novel index page URL")
    parser.add_argument("-o", "--output", help="Output EPUB filename")
    parser.add_argument(
        "--chapters",
        metavar="START-END",
        help="Chapter range, e.g. '1-50' or '10'",
    )
    parser.add_argument("--test", action="store_true", help="Parse index only, list chapters")
    parser.add_argument(
        "--test-chapter",
        type=int,
        metavar="N",
        help="Dump raw HTML of chapter N for debugging",
    )
    parser.add_argument(
        "--playwright",
        action="store_true",
        help="Force playwright mode (skip requests attempt)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)
    scraper = Scraper()

    # --test-chapter: dump raw HTML
    if args.test_chapter is not None:
        print(f"Parsing index: {args.url}")
        novel = scraper.parse_novel_index(args.url)
        if not novel or not novel.chapters:
            print("Failed to parse novel index.")
            sys.exit(1)
        n = args.test_chapter
        if n < 1 or n > len(novel.chapters):
            print(f"Chapter {n} out of range (1-{len(novel.chapters)})")
            sys.exit(1)
        chapter = novel.chapters[n - 1]
        print(f"\nFetching chapter {n}: {chapter.title}")
        print(f"URL: {chapter.url}")
        html = scraper.dump_raw_html(chapter.url)
        print("\n--- RAW HTML (first 3000 chars) ---")
        print(html[:3000])
        return

    # Parse index
    print(f"Fetching novel index: {args.url}")
    novel = scraper.parse_novel_index(args.url)
    if not novel:
        print("Failed to fetch or parse novel index.")
        sys.exit(1)

    print(f"\nTitle : 《{novel.title}》")
    print(f"Author: {novel.author}")
    print(f"Chapters: {len(novel.chapters)}")

    if not novel.chapters:
        print("No chapters found. Use --test-chapter 0 to inspect the page HTML.")
        sys.exit(1)

    # --test: list chapters and exit
    if args.test:
        print("\nChapter list (first 20):")
        for i, ch in enumerate(novel.chapters[:20], 1):
            print(f"  {i:4d}. {ch.title}")
        if len(novel.chapters) > 20:
            print(f"  ... and {len(novel.chapters) - 20} more")
        return

    # Determine chapter range
    start, end = 1, len(novel.chapters)
    if args.chapters:
        start, end = parse_chapter_range(args.chapters, len(novel.chapters))
    print(f"\nDownloading chapters {start}–{end} ({end - start + 1} chapters)...")

    scraper.fetch_chapters(novel, start=start, end=end, progress_callback=progress_bar)

    # Filter only fetched chapters
    fetched = [ch for ch in novel.chapters[start - 1 : end] if ch.content]
    if not fetched:
        print("No chapter content was downloaded.")
        sys.exit(1)

    # Build EPUB with only fetched chapters
    from copy import copy
    novel_copy = copy(novel)
    novel_copy.chapters = fetched

    output = build_epub(novel_copy, output_path=args.output)
    print(f"\nEPUB saved: {output}")
    print(f"Chapters included: {len(fetched)}")


if __name__ == "__main__":
    main()
