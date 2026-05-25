"""Provinces/cities without an open government speed-limit dataset (CN).

China does not publish per-segment posted speed limits as open data
at any level of government (verified by the 2026-05-25 survey
documented below). This module exists to capture that finding in
code rather than as a tribal-knowledge ad-hoc decision, mirroring
`au_unavailable.py` for the AU side.

Survey (2026-05-25):

  Ministry of Transport (交通运输部) -- data.mot.gov.cn
    - Open datasets cover highway statistics, freight indices, and
      project-level traffic counts.
    - No per-segment maxspeed / 限速 dataset published.
    - Provincial transport bureaus mirror the same pattern: bulk
      statistics + policy PDFs, never per-road speed attributes.

  Amap (高德地图) / Baidu Maps
    - Both vendors hold per-segment speed data internally and surface
      it through navigation but neither offers a bulk export.
    - Their open APIs return point-of-interest geocoding and routing
      results, not the underlying maxspeed attribute.
    - Commercial enterprise access exists (B2B SDK contract) but is
      outside the scope of a single-driver hobby project.
    - **Path to upgrade**: if a commercial agreement materialises,
      add a `cn_amap.py` fetcher that emits SpeedSegment with
      `source="AMAP"`, plumb a new bit in `src/speed_db.SRC_BIT_*`,
      and the cross_verify ladder will start producing
      OFFICIAL_VERIFIED automatically (OSM + Amap agreement).

  Provincial open data portals (Shanghai, Beijing, ...)
    - Each provincial 数据开放平台 publishes road inventory metadata
      (length, surface, ownership) but again no speed_limit column.
    - Shanghai's open data even includes a "Road Network" GeoJSON,
      but `Speed`-style fields are absent across spot checks of
      Pudong + Putuo districts.

Operational consequence:

  CN runs of `tools/build_db.py` rely on OSM alone. Per the policy
  decision recorded in `cross_verify.OFFICIAL_BITS_CN`, OSM-only
  segments land at CONFIDENCE_OFFICIAL_ONLY (not SINGLE_SOURCE),
  so the device renders a numeric limit instead of "--". When a
  second source becomes available the ladder restores its normal
  semantics without any device-side change.
"""
