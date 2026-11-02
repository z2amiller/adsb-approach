import pytest

from adsb_approach.altitude import (
    h_isa_ft, headwind_kt, qnh_altitude_ft, sigma, temperature_scaled_ft, true_from_magnetic,
)


def test_isa_height_of_standard_pressure_is_zero():
    assert h_isa_ft(1013.25) == pytest.approx(0.0, abs=1e-6)


def test_qnh_correction_matches_spec_example():
    # 1015.2 hPa -> pressure altitude reads ~53 ft low
    assert qnh_altitude_ft(1000, 1015.2) == pytest.approx(1053.2, abs=0.5)
    assert qnh_altitude_ft(1000, 1013.25) == pytest.approx(1000.0, abs=1e-6)


def test_temperature_scaling_warm_day():
    # 800 ft above a 13 ft station on a 21 C day: about +17 ft
    assert temperature_scaled_ft(813.0, 13.0, 21.0) == pytest.approx(829.7, abs=0.5)
    assert temperature_scaled_ft(813.0, 13.0, 15.0) == pytest.approx(813.0, abs=0.2)


def test_density_ratio():
    assert sigma(0) == pytest.approx(1.0)
    assert sigma(1000) == pytest.approx(0.9711, abs=0.0005)


def test_headwind_component():
    assert headwind_kt(280, 14, 298) == pytest.approx(13.31, abs=0.05)
    assert headwind_kt(118, 10, 298) == pytest.approx(-10.0, abs=0.01)
    assert headwind_kt(28, 10, 298) == pytest.approx(0.0, abs=0.01)


def test_true_from_magnetic():
    assert true_from_magnetic(284.0, 14.0) == 298.0
    assert true_from_magnetic(350.0, 14.0) == 4.0


def test_temperature_scaling_cold_day():
    # 800 ft above a 13 ft station on a 0 C day: about 42 ft lower than indicated
    assert temperature_scaled_ft(813.0, 13.0, 0.0) == pytest.approx(771.4, abs=0.5)
