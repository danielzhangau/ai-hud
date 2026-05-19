"""Solar day/night detection -- no third-party dependencies.

Implements the NOAA Solar Calculator approximation (1990). Instead of
returning sunrise/sunset clock times (which is awkward across date and
timezone boundaries), we compute the sun's altitude angle directly:

    altitude > 0      : sun above horizon (day)
    altitude < 0      : sun below horizon (night)
    altitude < -3 deg : ~30 min into dusk (HUD switches to night mode)

This avoids the cross-midnight / cross-timezone arithmetic that
sunrise/sunset-based code gets wrong at e.g. Beijing 21:00 local time.

Used by hud_live.py to decide day vs night from GPS-supplied UTC time +
latitude/longitude alone -- no RTC, no internet, no timezone database.
Accurate to ~1 minute up to 65 degrees latitude.
"""

import math


def _fractional_year_rad(date):
    """Approximate orbital position of Earth as 'gamma' (radians).

    `date` is a datetime.date in UTC.
    """
    n = date.timetuple().tm_yday
    # 365 is fine year-round; even leap-year accumulated error is <24s.
    return 2.0 * math.pi / 365.0 * (n - 1)


def _equation_of_time_min(gamma):
    """Equation of time (minutes); the sun-clock vs mean-clock offset."""
    return 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2.0 * gamma)
        - 0.040849 * math.sin(2.0 * gamma)
    )


def _solar_declination_rad(gamma):
    """Solar declination (radians)."""
    return (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2.0 * gamma)
        + 0.000907 * math.sin(2.0 * gamma)
        - 0.002697 * math.cos(3.0 * gamma)
        + 0.001480 * math.sin(3.0 * gamma)
    )


def solar_altitude_deg(date, time_minutes_utc, lat_deg, lon_deg):
    """Sun altitude above the horizon, in degrees.

    Inputs are taken straight from a GPS RMC fix: the UTC date, the UTC
    time-of-day in minutes (e.g. 13:00 UTC -> 780), and the lat/lon of
    the fix in decimal degrees (north positive, east positive).

    Positive values = sun above horizon (day).
    Negative values = sun below horizon (night).
    The standard refraction-corrected sunrise/sunset crossing is at ~-0.833.
    """
    gamma = _fractional_year_rad(date)
    decl = _solar_declination_rad(gamma)
    eqtime = _equation_of_time_min(gamma)

    # True solar time at this longitude (minutes since solar midnight).
    tst_min = (time_minutes_utc + eqtime + 4.0 * lon_deg) % 1440.0
    # Hour angle: 0 at solar noon, +180 at solar midnight.
    ha_deg = (tst_min / 4.0) - 180.0
    ha = math.radians(ha_deg)
    lat = math.radians(lat_deg)

    cos_zenith = (
        math.sin(lat) * math.sin(decl)
        + math.cos(lat) * math.cos(decl) * math.cos(ha)
    )
    # Clamp to guard against FP drift outside [-1, 1].
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    return 90.0 - math.degrees(math.acos(cos_zenith))


# Threshold tuned for dashcam HUD: switch to night mode about 30 minutes
# after the geometric sunset. Earlier (closer to 0) would make the
# screen too dim during golden hour; later (more negative) would leave
# the user with a daylight HUD when ambient light has clearly faded.
_NIGHT_ALTITUDE_THRESHOLD_DEG = -3.0


def is_night_at(date, time_minutes_utc, lat_deg, lon_deg):
    """True if the sun is below the night-mode altitude threshold.

    Returns None when GPS coordinates aren't available yet -- the caller
    should hold its previous day/night decision in that case rather than
    flapping.
    """
    if lat_deg is None or lon_deg is None:
        return None
    alt = solar_altitude_deg(date, time_minutes_utc, lat_deg, lon_deg)
    return alt < _NIGHT_ALTITUDE_THRESHOLD_DEG


def is_night_now(utc_now, lat_deg, lon_deg):
    """is_night_at() but takes a datetime.datetime instead of (date, minutes)."""
    if utc_now is None:
        return None
    minutes = utc_now.hour * 60.0 + utc_now.minute + utc_now.second / 60.0
    return is_night_at(utc_now.date(), minutes, lat_deg, lon_deg)
