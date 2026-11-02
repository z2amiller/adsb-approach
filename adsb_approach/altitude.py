"""Altimetry and wind helpers. Feet, hPa, degrees C, knots."""
from __future__ import annotations

import math

P0_HPA = 1013.25


def h_isa_ft(p_hpa: float) -> float:
    """ISA height of pressure level p (ft); 0 at 1013.25 hPa, negative above it."""
    return 145366.45 * (1.0 - (p_hpa / P0_HPA) ** 0.190284)


def qnh_altitude_ft(alt_baro_ft: float, qnh_hpa: float) -> float:
    """Pressure altitude corrected to the local altimeter setting."""
    return alt_baro_ft - h_isa_ft(qnh_hpa)


def isa_temp_c(elev_ft: float) -> float:
    return 15.0 - 0.0019812 * elev_ft


def temperature_scaled_ft(alt_qnh_ft: float, station_elev_ft: float, temp_c: float) -> float:
    """Scale height above the station by actual/ISA absolute temperature (warm day: higher)."""
    t_isa_k = isa_temp_c(station_elev_ft) + 273.15
    t_k = temp_c + 273.15
    return station_elev_ft + (alt_qnh_ft - station_elev_ft) * (t_k / t_isa_k)


def sigma(alt_ft: float) -> float:
    """ISA density ratio; IAS ~ TAS * sqrt(sigma)."""
    return (1.0 - 6.875e-6 * alt_ft) ** 4.256


def headwind_kt(wind_dir_deg: float, wind_kt: float, course_true_deg: float) -> float:
    """Positive when the wind blows against the direction of flight."""
    return wind_kt * math.cos(math.radians(wind_dir_deg - course_true_deg))


def true_from_magnetic(mag_deg: float, declination_east_deg: float) -> float:
    return (mag_deg + declination_east_deg) % 360.0
