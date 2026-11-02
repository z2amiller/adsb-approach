"""Spherical-earth geometry around a runway threshold. Feet and degrees throughout."""
from __future__ import annotations

import math

from .model import RunwayEnd

FT_PER_M = 3.280839895
NM_FT = 6076.115
R_EARTH_FT = 6_371_000.0 * FT_PER_M


def enu_ft(lat0: float, lon0: float, lat: float, lon: float) -> tuple[float, float]:
    """East/north offset in feet of (lat, lon) from (lat0, lon0) on an azimuthal equidistant projection
    about (lat0, lon0): distance and bearing from the origin are exact on the sphere, so great circles
    through it (extended runway centrelines, localizer radials) are straight lines."""
    d = distance_ft(lat0, lon0, lat, lon)
    b = math.radians(bearing_deg(lat0, lon0, lat, lon))
    return d * math.sin(b), d * math.cos(b)


def distance_ft(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R_EARTH_FT * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(x, y)) % 360.0


def destination(lat: float, lon: float, bearing: float, dist_ft: float) -> tuple[float, float]:
    ang = dist_ft / R_EARTH_FT
    p1, l1, b = math.radians(lat), math.radians(lon), math.radians(bearing)
    p2 = math.asin(math.sin(p1) * math.cos(ang) + math.cos(p1) * math.sin(ang) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(ang) * math.cos(p1), math.cos(ang) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), math.degrees(l2)


def track_diff_deg(a: float, b: float) -> float:
    """Signed smallest difference a - b in degrees, in [-180, 180)."""
    return (a - b + 180.0) % 360.0 - 180.0


def runway_frame(rwy: RunwayEnd, lat: float, lon: float) -> tuple[float, float]:
    """(along_ft, cross_ft): along > 0 on the approach side of the landing threshold,
    cross > 0 to the right of the inbound course as the pilot sees it."""
    e, n = enu_ft(rwy.threshold_lat, rwy.threshold_lon, lat, lon)
    c = math.radians(rwy.course_true_deg)
    ue, un = math.sin(c), math.cos(c)        # unit vector in the direction of flight
    re, rn = math.cos(c), -math.sin(c)       # unit vector to the right of it
    along = -(e * ue + n * un)
    cross = e * re + n * rn
    return along, cross


def far_end(rwy: RunwayEnd) -> tuple[float, float]:
    """The end of the pavement: CIFP thresholds are landing thresholds, so the far end lies the runway length less this
    end's displaced threshold beyond it."""
    return destination(rwy.threshold_lat, rwy.threshold_lon, rwy.course_true_deg, rwy.landing_length_ft)


def localizer_position(rwy: RunwayEnd) -> tuple[float, float]:
    if rwy.localizer_lat is not None and rwy.localizer_lon is not None:
        return rwy.localizer_lat, rwy.localizer_lon
    return destination(rwy.threshold_lat, rwy.threshold_lon, rwy.course_true_deg,
                       rwy.landing_length_ft + rwy.localizer_offset_ft)


def localizer_to_threshold_ft(rwy: RunwayEnd) -> float:
    lat, lon = localizer_position(rwy)
    return distance_ft(lat, lon, rwy.threshold_lat, rwy.threshold_lon)


def loc_width_deg(rwy: RunwayEnd) -> float:
    """Full localizer course width; CIFP value if known, else tailored to 700 ft at the threshold."""
    if rwy.loc_width_deg:
        return rwy.loc_width_deg
    return math.degrees(2 * math.atan(350.0 / localizer_to_threshold_ft(rwy)))


def gpip_along_ft(rwy: RunwayEnd) -> float:
    """Along-track position of the glide-path intercept point (negative: past the threshold)."""
    return -rwy.tch_ft / math.tan(math.radians(rwy.glide_path_deg))
