"""tar1090-db aircraft lookup (hex -> registration, type) and traffic classification."""
from __future__ import annotations

import gzip
import re
from dataclasses import dataclass
from pathlib import Path

from .config import TypeClasses

AIRLINE_CALLSIGN = re.compile(r"^[A-Z]{3}[0-9]{1,4}[A-Z]{0,2}$")
HEAVY_CATEGORIES = {"A3", "A4", "A5"}


@dataclass(slots=True)
class AircraftInfo:
    hex: str
    reg: str | None
    type_code: str | None
    flags: str | None
    desc: str | None


class TypeDb:
    def __init__(self) -> None:
        self._by_hex: dict[str, AircraftInfo] = {}

    @classmethod
    def load(cls, path: str | Path) -> "TypeDb":
        db = cls()
        opener = gzip.open if str(path).endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8", errors="replace") as fh:   # type: ignore[operator]
            for line in fh:
                parts = line.rstrip("\n").split(";")
                if len(parts) < 3 or not parts[0]:
                    continue
                hexid = parts[0].strip().lower()
                db._by_hex[hexid] = AircraftInfo(
                    hex=hexid, reg=parts[1].strip() or None, type_code=parts[2].strip() or None,
                    flags=parts[3].strip() if len(parts) > 3 and parts[3].strip() else None,
                    desc=parts[4].strip() if len(parts) > 4 and parts[4].strip() else None,
                )
        return db

    def get(self, hexid: str) -> AircraftInfo | None:
        return self._by_hex.get(hexid.lower())

    def __len__(self) -> int:
        return len(self._by_hex)


def classify(callsign: str | None, type_code: str | None, category: str | None, classes: TypeClasses) -> str:
    """Traffic class. Category A7 (rotorcraft) is checked first, ahead of type and callsign. Otherwise a bizjet type
    wins over an airline-shaped callsign: charter and fractional operators (EJA, LXJ, VJA) fly bizjets under ICAO
    telephony designators, and large-cabin bizjets report category A3."""
    if (category or "").upper() == "A7":
        return "rotorcraft"
    if classes.is_bizjet(type_code):
        return "bizjet"
    cs = (callsign or "").strip().upper()
    if cs and AIRLINE_CALLSIGN.match(cs) and (classes.is_airliner(type_code) or (category or "").upper() in HEAVY_CATEGORIES):
        return "airline"
    return "other"
