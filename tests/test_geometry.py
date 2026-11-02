import math

import pytest

from adsb_approach.geometry import (
    NM_FT, bearing_deg, destination, distance_ft, far_end, gpip_along_ft, loc_width_deg,
    localizer_position, runway_frame, track_diff_deg,
)
from adsb_approach.model import RunwayEnd


def sfo_28r(**over) -> RunwayEnd:
    base = dict(
        airport="KSFO", ident="28R", enabled=True,
        threshold_lat=37.613919, threshold_lon=-122.358058, threshold_elev_ft=13.0, threshold_hae_m=-28.7,
        course_true_deg=298.0, runway_length_ft=11870.0, displaced_threshold_ft=300.0,
        localizer_lat=37.629544, localizer_lon=-122.395311, localizer_offset_ft=1000.0,
        loc_width_deg=3.29, glide_path_deg=3.0, tch_ft=55.0, ils="IGWQ 111.70 CAT III",
        reference_source="cifp:CIFP_260903", parallel_group=("28L", "28R"),
    )
    base.update(over)
    return RunwayEnd(**base)


def test_point_three_miles_out_on_centreline():
    rwy = sfo_28r()
    lat, lon = destination(rwy.threshold_lat, rwy.threshold_lon, (rwy.course_true_deg + 180) % 360, 3 * NM_FT)
    along, cross = runway_frame(rwy, lat, lon)
    assert along == pytest.approx(3 * NM_FT, abs=5)
    assert abs(cross) < 5


def test_cross_track_positive_to_the_right_of_course():
    rwy = sfo_28r()
    lat, lon = destination(rwy.threshold_lat, rwy.threshold_lon, (rwy.course_true_deg + 180) % 360, 2 * NM_FT)
    lat_r, lon_r = destination(lat, lon, (rwy.course_true_deg + 90) % 360, 750)
    along, cross = runway_frame(rwy, lat_r, lon_r)
    assert cross == pytest.approx(750, abs=5)
    assert along == pytest.approx(2 * NM_FT, abs=5)


def test_28l_threshold_is_750ft_left_of_28r():
    rwy = sfo_28r()
    along, cross = runway_frame(rwy, 37.612097, -122.359264)   # KSFO 28L landing threshold (CIFP)
    assert cross == pytest.approx(-750, abs=25)
    assert abs(along) < 400


def test_distance_and_bearing_between_thresholds():
    assert distance_ft(37.613919, -122.358058, 37.612097, -122.359264) == pytest.approx(750, abs=25)
    assert bearing_deg(37.613919, -122.358058, 37.629544, -122.395311) == pytest.approx(298, abs=1.5)


def test_track_diff_is_signed_and_wrapped():
    assert track_diff_deg(350, 10) == -20
    assert track_diff_deg(10, 350) == 20
    assert track_diff_deg(180, 180) == 0
    assert track_diff_deg(0, 180) == -180 and track_diff_deg(180, 0) == -180


def test_glide_path_intercept_and_localizer_width():
    rwy = sfo_28r()
    assert gpip_along_ft(rwy) == pytest.approx(-55 / math.tan(math.radians(3.0)), abs=0.1)
    assert loc_width_deg(rwy) == 3.29
    fallback = sfo_28r(localizer_lat=None, localizer_lon=None, loc_width_deg=None)
    lat, lon = localizer_position(fallback)
    # 1,000 ft beyond the far end, which lies 11,870 ft less the 300 ft displaced threshold past the landing threshold
    assert distance_ft(lat, lon, rwy.threshold_lat, rwy.threshold_lon) == pytest.approx(12570, abs=15)
    assert loc_width_deg(fallback) == pytest.approx(math.degrees(2 * math.atan(350 / 12570)), abs=0.01)


def test_far_end_is_the_end_of_the_pavement():
    # KSFO 28R lands 300 ft past its runway end; its far end is 10L's landing threshold, which is not displaced
    rwy = sfo_28r(course_true_deg=297.914)
    lat, lon = far_end(rwy)
    assert distance_ft(rwy.threshold_lat, rwy.threshold_lon, lat, lon) == pytest.approx(11570, abs=1)
    assert distance_ft(lat, lon, 37.628739, -122.393392) < 25          # CIFP PG record for 10L


def test_centreline_point_ten_miles_out_has_no_cross_track_error():
    rwy = sfo_28r()
    lat, lon = destination(rwy.threshold_lat, rwy.threshold_lon, (rwy.course_true_deg + 180) % 360, 10 * NM_FT)
    along, cross = runway_frame(rwy, lat, lon)
    assert along == pytest.approx(10 * NM_FT, abs=1)
    assert abs(cross) < 1
