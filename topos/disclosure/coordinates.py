"""Where a journal entry was written: exact coordinates, readable by the owner only (DISCLOSURE_RULES DR-77).

Grow's phone app can record the position an entry was posted from. Its Cloud Function adds four fields to a
``grow_journal`` app_ingest record when the entry has one, and leaves them out otherwise:

- ``latitude`` and ``longitude``: decimal degrees (WGS84);
- ``location_accuracy_m``: the phone's accuracy radius, in metres;
- ``location_taken_at``: when the position was taken, ISO 8601 in UTC (``+00:00``).

The node keeps them exact, on the entry's own ``journal_entries`` row under the same four names
(migration ``journal_entries_coordinates_v1``), and nowhere derived: no embedding, index, fact, statistic or
location fan-out reads them. The raw retention table keeps the payload as it arrived, as it does every field.

Owner rulings, 18 Nov 2026:

1. Visibility, "Only me": the owner's own surface reads them. An app with a journal grant, a shared view, an
   outside AI client, a routine and the owner's coding agent do not, until a later ruling opens them. Every door
   short of the owner's own app (principal class ``owner_app``) is refused, including a caller with no principal,
   the way the table tools already treat owner-only tables (``data_explorer_tables``).
2. Precision: "Make the exact option, with rounded options too (like how the dates can be transformed. We want the
   option to limit precision to others.)" The exact value is stored; ``PRECISION_TRANSFORMS`` are grant field
   transforms, beside ``timestamp_to_date``, that coarsen a shared position. While ``COORDINATES_SHAREABLE`` is
   False they never reach a grantee, because the grantee paths drop the columns before any transform runs.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Mapping, Optional

LATITUDE = "latitude"
LONGITUDE = "longitude"
ACCURACY = "location_accuracy_m"
TAKEN_AT = "location_taken_at"

#: The incoming record fields and the ``journal_entries`` columns: one set of names on both sides.
COORDINATE_COLUMNS: tuple[str, ...] = (LATITUDE, LONGITUDE, ACCURACY, TAKEN_AT)

#: DR-77's switch. False (the owner's ruling, 18 Nov 2026): no grantee path carries a coordinate column, whatever
#: transform a grant names. Opening it is a code change and a new ruling: the owner would also need a way to pick a
#: precision per grant, which no screen offers today.
COORDINATES_SHAREABLE = False

#: Grant field transforms that coarsen a position, as ``timestamp_to_date`` coarsens a time. The value is the number
#: of decimal places kept: 0.001 degree of latitude is about 111 m, 0.01 about 1.1 km and 0.1 about 11 km (a degree
#: of longitude is shorter away from the equator, so the cell is narrower east to west, never wider).
PRECISION_TRANSFORMS: Dict[str, int] = {
    "coordinates_to_100m": 3,
    "coordinates_to_1km": 2,
    "coordinates_to_10km": 1,
}

#: The radius each transform claims, in metres. A coarsened row never reports a finer accuracy than its cell.
PRECISION_METRES: Dict[str, float] = {
    "coordinates_to_100m": 100.0,
    "coordinates_to_1km": 1_000.0,
    "coordinates_to_10km": 10_000.0,
}


def _finite_number(value: Any) -> Optional[float]:
    """A finite float from a JSON number or a numeric string; None for anything else (booleans included)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def _iso_instant(value: Any) -> Optional[str]:
    """The text as sent when it parses as an ISO 8601 date-time; None otherwise. Stored exact, never re-zoned."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return text


def coordinate_fields(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """The four coordinate fields of an incoming record, validated; all four None when there is no usable position.

    A position needs both halves inside their ranges (latitude -90..90, longitude -180..180). Without one, nothing
    is kept: an accuracy or a time with no position describes nothing. The accuracy must be a finite number of
    metres, zero or more, and the time an ISO 8601 date-time; either is dropped on its own when it is not.
    """
    empty: Dict[str, Any] = {name: None for name in COORDINATE_COLUMNS}
    latitude = _finite_number(payload.get(LATITUDE))
    longitude = _finite_number(payload.get(LONGITUDE))
    if latitude is None or longitude is None or not -90.0 <= latitude <= 90.0 or not -180.0 <= longitude <= 180.0:
        return empty
    accuracy = _finite_number(payload.get(ACCURACY))
    return {
        LATITUDE: latitude,
        LONGITUDE: longitude,
        ACCURACY: accuracy if accuracy is not None and accuracy >= 0 else None,
        TAKEN_AT: _iso_instant(payload.get(TAKEN_AT)),
    }


def has_position(record: Mapping[str, Any]) -> bool:
    return record.get(LATITUDE) is not None and record.get(LONGITUDE) is not None


# ------------------------------------------------------------------ who reads them


def owner_reads_coordinates() -> bool:
    """True only for the owner's own surface (the channel-verified ``owner_app`` class).

    A third-party client, a CP relay call without a stamp, the owner's coding agent and a caller with no principal
    are all refused, the same test ``data_explorer_tables.hidden_from_current_principal`` applies to owner-only
    tables.
    """
    from ..principal import OWNER_APP, current_principal

    return getattr(current_principal(), "cls", None) == OWNER_APP


def withhold_coordinates(rows: Iterable[Any]) -> List[Any]:
    """The rows without the four coordinate keys, and without them inside a raw ``payload_json``.

    By name, on every table: no node table had a column of these names before this one, and a source table the
    control plane declares with them (``grow_journal_sessions``, if it ever mirrors them) holds the same values. A
    raw payload that cannot be parsed while naming a key is withheld whole rather than served.
    """
    out: List[Any] = []
    for row in rows:
        if not isinstance(row, dict):
            out.append(row)
            continue
        kept = {key: value for key, value in row.items() if key not in COORDINATE_COLUMNS}
        payload = kept.get("payload_json")
        if isinstance(payload, str) and any(name in payload for name in COORDINATE_COLUMNS):
            kept["payload_json"] = _payload_without_coordinates(payload)
        out.append(kept)
    return out


def _payload_without_coordinates(text: str) -> Optional[str]:
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    if not any(name in parsed for name in COORDINATE_COLUMNS):
        return text  # the word appears in the text only, not as a field: served as stored
    return json.dumps({key: value for key, value in parsed.items() if key not in COORDINATE_COLUMNS},
                      ensure_ascii=False)


def grantee_rows(rows: Iterable[Any]) -> List[Any]:
    """What a grantee path may carry: every row without the coordinate keys while DR-77 keeps them owner-only."""
    if COORDINATES_SHAREABLE:
        return list(rows)
    return withhold_coordinates(rows)


# ------------------------------------------------------------------ precision transforms


def _round(value: float, places: int) -> float:
    try:
        quantum = Decimal(1).scaleb(-places)
        return float(Decimal(repr(float(value))).quantize(quantum, rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("coordinate is not a number") from None


def round_position(row: Mapping[str, Any], transform_id: str) -> Dict[str, Any]:
    """The row with its position coarsened by one of ``PRECISION_TRANSFORMS``.

    Both halves round together, whichever one the grant names: a position with one exact half is still a line
    through the owner's door. The accuracy becomes at least the cell's radius, so the row never claims a precision
    it no longer has. ``location_taken_at`` is a time, and coarsens with ``timestamp_to_date`` like any other.
    A row with no position, or one half missing, comes back without either half.
    """
    places = PRECISION_TRANSFORMS[transform_id]
    out = dict(row)
    latitude = _finite_number(out.get(LATITUDE))
    longitude = _finite_number(out.get(LONGITUDE))
    if latitude is None or longitude is None:
        for name in (LATITUDE, LONGITUDE, ACCURACY):
            out.pop(name, None)
        return out
    out[LATITUDE] = _round(latitude, places)
    out[LONGITUDE] = _round(longitude, places)
    radius = PRECISION_METRES[transform_id]
    accuracy = _finite_number(out.get(ACCURACY))
    out[ACCURACY] = max(accuracy, radius) if accuracy is not None else radius
    return out


def is_precision_transform(transform_id: Any) -> bool:
    return isinstance(transform_id, str) and transform_id in PRECISION_TRANSFORMS
