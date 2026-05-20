"""Per-source speed-limit fetchers.

Each fetcher converts an upstream dataset (state government open data,
OSM, etc.) into a stream of `SpeedSegment` records described by `ir.py`.

Design contract -- every fetcher exposes:
    class XYZFetcher(SourceFetcher):
        name = "AU_XYZ_GOV"
        state = "XYZ"
        def fetch(self, limit=None, archive=True) -> list[SpeedSegment]: ...

Run a fetcher standalone for smoke testing:
    python3 -m tools.fetchers.au_nsw --limit 1000
    python3 -m tools.fetchers.au_wa  --limit 1000
"""
