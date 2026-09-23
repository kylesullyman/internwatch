from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from .models import Job

PRUNE_AFTER_DAYS = 200   # long enough that a posting can't resurface as "new"


class State:
    """Seen-job keys + which sources have been initialized.

    The first time a source is polled, everything it returns is recorded silently;
    otherwise the first run (or adding a company) would fire thousands of alerts.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        data = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.seen: dict[str, float] = data.get("seen", {})
        self.sources: dict[str, float] = data.get("sources", {})
        self.dirty = False

    @staticmethod
    def _h(key: str) -> str:
        # 64-bit hashes keep the file ~4x smaller than raw URLs; collisions are negligible here.
        return hashlib.blake2b(key.encode(), digest_size=8).hexdigest()

    def is_new(self, job: Job) -> bool:
        return not any(self._h(k) in self.seen for k in job.keys)

    def mark(self, job: Job, now: float | None = None) -> None:
        now = int(now or time.time())
        for k in job.keys:
            self.seen.setdefault(self._h(k), now)
        self.dirty = True

    def initialized(self, source_id: str) -> bool:
        return source_id in self.sources

    def set_initialized(self, source_id: str) -> None:
        self.sources[source_id] = int(time.time())
        self.dirty = True

    def save(self) -> None:
        cutoff = time.time() - PRUNE_AFTER_DAYS * 86400
        self.seen = {k: v for k, v in self.seen.items() if v >= cutoff}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"sources": self.sources, "seen": self.seen}, separators=(",", ":")))
        os.replace(tmp, self.path)
