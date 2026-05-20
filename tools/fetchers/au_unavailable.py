"""States/territories without an open speed-limit dataset.

This module documents the negative findings from the 2026-05-20
endpoint survey so future contributors don't waste effort re-probing
the same sources. When an upstream becomes available, replace the
matching stub with a real fetcher and link to it from the YAML config.

Current status (2026-05-20):

  TAS
    - data.tas.gov.au has no standalone speed-zone or per-segment
      maxspeed layer.
    - Department of State Growth holds the data internally; access
      requires direct contact.
    - Fallback: rely on OSM coverage.

  NT
    - data.nt.gov.au "NT Government Controlled Roads" dataset exists
      but is published only as KMZ for map-viewer use and *does not
      include speed limit attributes* (confirmed via the upstream
      metadata, 2026-05-20).
    - Fallback: rely on OSM coverage.

  SA
    - data.sa.gov.au "Roads" GeoJSON contains class / surface /
      one_way fields but NO speed_limit / maxspeed attribute (verified
      against Roads_GDA94.geojson, 605 MB, last refreshed 2026-01-24).
    - DIT publishes only a policy PDF ("Speed Limit Guideline for
      South Australia"), not a spatial dataset.
    - Travel Speed dataset measures observed speeds, not posted limits.
    - Fallback: rely on OSM coverage.

The cross_verify stage (Sprint 2) treats these regions as
"OSM-only -- low confidence" by default, which under the user's
selected policy ("官方源优先 + OSM 仅作交叉验证") means the device
will display "--" instead of a numeric limit when only OSM exists.
"""
