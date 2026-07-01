#!/usr/bin/env python3
"""
units.py - thin unit-conversion wrapper used by ragkit.py's filter validator
(validate_filter in ragkit.py) to convert a query filter's bound into a
catalogue field's canonical unit before comparing.

Backed by pint when available. If pint is missing, every convert() call
raises ConversionError so callers fail safe (drop the filter) instead of
silently comparing mismatched magnitudes -- see ragkit.py's comment at the
validate_filter call site ("dimensional mismatch / unconvertible / pint
missing: drop the whole filter for this field").
"""

try:
    import pint
    _ureg = pint.UnitRegistry(autoconvert_offset_to_baseunit=True)
except Exception:
    # Broad except deliberately: this environment's pint pulls in dask,
    # which fails to import under numpy 2.0 (AttributeError, not
    # ImportError) -- any failure here should degrade to "pint
    # unavailable" rather than take ragkit.py down at import time.
    pint = None
    _ureg = None


class ConversionError(Exception):
    pass


# Aliases for unit spellings this dataset/pipeline uses that pint doesn't
# recognize under the same name out of the box.
_ALIASES = {
    "nmi": "nautical_mile",
    "kn": "knot",
    "kts": "knot",
    "deg": "degree",
}


def _resolve(unit):
    if not unit:
        raise ConversionError("empty unit")
    key = unit.strip()
    return _ALIASES.get(key.lower(), key)


def normalize_unit(unit):
    """Canonical string form of a unit, for equality comparison without a
    full conversion (e.g. deciding whether a filter's unit already matches
    a field's canonical unit)."""
    if not unit:
        return ""
    if _ureg is None:
        return unit.strip().lower()
    try:
        return str(_ureg.parse_units(_resolve(unit)))
    except Exception:
        return unit.strip().lower()


def convert(value, from_unit, to_unit):
    if _ureg is None:
        raise ConversionError("pint is not installed; unit conversion unavailable")
    try:
        qty = _ureg.Quantity(value, _resolve(from_unit))
        return qty.to(_resolve(to_unit)).magnitude
    except ConversionError:
        raise
    except Exception as e:
        raise ConversionError(f"cannot convert {from_unit!r} to {to_unit!r}: {e}")
