"""Working-directory lifecycle for downloaded media.

Media files are large, temporary, and must never reach git. Two modes:

  * **ephemeral** (default) — a temp directory removed on exit, whatever
    happens. Right for CI, where the runner is thrown away anyway, and right
    for local runs where leftover gigabytes are a nuisance.
  * **persistent** — a configured directory that survives, so a download can
    be inspected or reused across commands while developing.

Cleanup is in a ``finally``, so an exception mid-pipeline does not leak a
half-downloaded file.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger(__name__)


@contextmanager
def work_dir(
    configured: Optional[str] = None, keep: bool = False
) -> Iterator[Path]:
    """Yield a working directory, cleaning it up unless asked not to.

    ``configured`` empty/None gives a temp directory. ``keep`` leaves whatever
    was created in place — for debugging a bad download.
    """
    if configured:
        path = Path(configured).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        created = False
    else:
        path = Path(tempfile.mkdtemp(prefix="f1clipper-"))
        created = True

    try:
        yield path
    finally:
        if created and not keep:
            shutil.rmtree(path, ignore_errors=True)
            logger.debug("removed working directory %s", path)
        elif keep:
            logger.info("keeping working directory %s", path)


def dir_size_bytes(path: Path) -> int:
    """Total size of files under a directory. Used for logging, not limits."""
    return sum(p.stat().st_size for p in Path(path).rglob("*") if p.is_file())
