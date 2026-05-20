"""Abstract base for all source fetchers.

Subclasses must:
  * set `name` (logical source id, e.g. "AU_NSW_GOV")
  * set `state` (state code, e.g. "NSW")
  * implement `fetch()` returning a list[SpeedSegment]

Keeping this interface tiny on purpose -- per ISP, the cross-verify
stage only needs a flat record stream; everything else (paging,
schema, archival) is the fetcher's own business.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from .ir import SpeedSegment


class SourceFetcher(ABC):
    name: str = ""
    state: str = ""

    @abstractmethod
    def fetch(self, limit: int | None = None,
              archive: bool = True) -> list[SpeedSegment]:
        """Return all SpeedSegment records this source can produce.

        Args:
            limit:   early-stop after N segments. Only honoured at
                     feature granularity, so the actual count may
                     slightly exceed `limit`. Used for local smoke
                     tests; production runs leave it as None.
            archive: if True, also write a gzipped raw snapshot to
                     data/raw/au/<state>/... before parsing.
        """
        ...
