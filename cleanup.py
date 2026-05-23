"""
Disk cleanup for generated EPUBs.

Two layers:
  1. Caller (download endpoint) deletes a file immediately after streaming.
  2. Background sweeper deletes any file older than EPUB_TTL_SECONDS (default 24h)
     in case a job completed but the user never downloaded.
"""
import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 86400      # 24 hours
DEFAULT_SWEEP_INTERVAL = 1800    # 30 minutes


def cleanup_expired(epubs_dir: str, max_age_seconds: int = DEFAULT_TTL_SECONDS) -> int:
    """Delete *.epub files in epubs_dir older than max_age_seconds.
    Returns count of files removed."""
    if not os.path.isdir(epubs_dir):
        return 0
    now = time.time()
    removed = 0
    for name in os.listdir(epubs_dir):
        if not name.endswith(".epub"):
            continue
        path = os.path.join(epubs_dir, name)
        try:
            age = now - os.path.getmtime(path)
            if age > max_age_seconds:
                os.unlink(path)
                removed += 1
                logger.info(f"cleanup: removed {name} (age={int(age)}s)")
        except OSError as e:
            logger.warning(f"cleanup: failed to stat/remove {name}: {e}")
    return removed


def start_background_sweeper(
    epubs_dir: str,
    max_age_seconds: int = DEFAULT_TTL_SECONDS,
    interval_seconds: int = DEFAULT_SWEEP_INTERVAL,
) -> threading.Thread:
    """Start a daemon thread that runs cleanup_expired every interval_seconds."""
    def _loop():
        while True:
            try:
                cleanup_expired(epubs_dir, max_age_seconds)
            except Exception as e:
                logger.error(f"cleanup sweep error: {e}")
            time.sleep(interval_seconds)

    t = threading.Thread(target=_loop, name="epub-cleanup", daemon=True)
    t.start()
    logger.info(
        f"cleanup sweeper started: dir={epubs_dir} ttl={max_age_seconds}s interval={interval_seconds}s"
    )
    return t


def safe_unlink(path: Optional[str]) -> bool:
    """Delete file if it exists; never raises."""
    if not path:
        return False
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        logger.warning(f"safe_unlink failed for {path}: {e}")
        return False
