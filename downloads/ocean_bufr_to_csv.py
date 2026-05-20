#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import os
import sys
import contextlib
from pathlib import Path
from typing import Iterable


LAT_KEYS = [
    "latitude",
    "stationLatitude",
    "shipOrMobileLandStationLatitude",
    "buoyOrPlatformLatitude",
]

LON_KEYS = [
    "longitude",
    "stationLongitude",
    "shipOrMobileLandStationLongitude",
    "buoyOrPlatformLongitude",
]

# Common metadata / noisy internal keys to exclude from the variables list.
# (This is intentionally conservative; you can tweak as needed.)
EXCLUDE_KEYS = {
    "unpack",
    "skipExtraKeyAttributes",
    # Message/header-ish
    "edition",
    "masterTableNumber",
    "localTableNumber",
    "masterTablesVersionNumber",
    "localTablesVersionNumber",
    "bufrHeaderCentre",
    "bufrHeaderSubCentre",
    "dataCategory",
    "internationalDataSubCategory",
    "dataSubCategory",
    "typicalYear",
    "typicalMonth",
    "typicalDay",
    "typicalHour",
    "typicalMinute",
    "typicalSecond",
    "typicalDate",
    "typicalTime",
    "numberOfSubsets",
    "compressedData",
    "observedData",
    "messageLength",
    # Lat/Lon (we keep them as separate columns)
    *LAT_KEYS,
    *LON_KEYS,
}


@contextlib.contextmanager
def _suppress_c_stderr(enabled: bool = True):
    """Suppress C-level stderr (used by ecCodes for noisy warnings)."""

    if not enabled:
        yield
        return

    old_stderr = os.dup(2)
    try:
        with open(os.devnull, "w") as devnull:
            os.dup2(devnull.fileno(), 2)
            yield
    finally:
        os.dup2(old_stderr, 2)
        os.close(old_stderr)


def _eccodes_available() -> bool:
    try:
        import eccodes  # noqa: F401

        return True
    except Exception:
        return False


def _iter_bufr_files(root: Path) -> Iterable[Path]:
    # Accept both .bufr and .bufr4
    exts = ("*.bufr", "*.bufr4")
    for pattern in exts:
        yield from root.rglob(pattern)


def _safe_is_missing(eccodes, h, key: str) -> bool:
    try:
        return bool(eccodes.codes_is_missing(h, key))
    except Exception:
        return True


def _safe_get_first_number(eccodes, h, key: str):
    """Return a scalar numeric value for key.

    If the key is an array, returns the first non-missing element.
    """

    try:
        size = eccodes.codes_get_size(h, key)
    except Exception:
        return None

    if size <= 0:
        return None

    if size == 1:
        try:
            if _safe_is_missing(eccodes, h, key):
                return None
            return eccodes.codes_get(h, key)
        except Exception:
            return None

    try:
        arr = eccodes.codes_get_array(h, key)
    except Exception:
        return None

    if arr is None:
        return None

    for val in arr:
        if val is None:
            continue
        try:
            # ecCodes uses very large numbers for missing sometimes; also NaN possible.
            if isinstance(val, float) and (val != val):
                continue
        except Exception:
            pass
        return val

    return None


def _extract_lat_lon_and_vars(path: Path, *, max_vars: int | None) -> tuple[float | None, float | None, list[str]]:
    import eccodes

    lat = None
    lon = None
    vars_seen: set[str] = set()

    with _suppress_c_stderr(True):
        with path.open("rb") as f:
            while True:
                h = eccodes.codes_bufr_new_from_file(f)
                if h is None:
                    break
                try:
                    eccodes.codes_set(h, "unpack", 1)
                    try:
                        eccodes.codes_set(h, "skipExtraKeyAttributes", 1)
                    except Exception:
                        pass

                if lat is None:
                    for k in LAT_KEYS:
                        v = _safe_get_first_number(eccodes, h, k)
                        if v is not None:
                            try:
                                vv = float(v)
                            except Exception:
                                continue
                            if -90.0 <= vv <= 90.0:
                                lat = vv
                                break

                if lon is None:
                    for k in LON_KEYS:
                        v = _safe_get_first_number(eccodes, h, k)
                        if v is not None:
                            try:
                                vv = float(v)
                            except Exception:
                                continue
                            # Accept either [-180,180] or [0,360]
                            if -180.0 <= vv <= 180.0 or 0.0 <= vv <= 360.0:
                                lon = vv
                                break

                    it = eccodes.codes_keys_iterator_new(h)
                    try:
                        while eccodes.codes_keys_iterator_next(it):
                            key = eccodes.codes_keys_iterator_get_name(it)
                            if not key:
                                continue
                            if key.startswith("#"):
                                continue
                            if key in EXCLUDE_KEYS:
                                continue

                            lk = key.lower()
                            if "sequence" in lk or lk == "sequences" or lk.endswith("hash"):
                                continue

                        # Try to exclude clearly internal / section keys.
                        if key.startswith("section"):
                            continue
                        if key.startswith("typical"):
                            continue

                        # Only keep keys that are actually present (not missing everywhere).
                        if _safe_is_missing(eccodes, h, key):
                            continue

                            vars_seen.add(key)
                            if max_vars is not None and len(vars_seen) >= max_vars:
                                break
                    finally:
                        eccodes.codes_keys_iterator_delete(it)

                finally:
                    eccodes.codes_release(h)

            if max_vars is not None and len(vars_seen) >= max_vars:
                # We already hit the cap; no need to decode further messages.
                break

    return lat, lon, sorted(vars_seen)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Loop through BUFR files under a root path and create one CSV row per file: "
            "latitude, longitude, variables."
        )
    )
    parser.add_argument(
        "--root",
        default="/Users/anujdivesh/Desktop/wis2.0/final/downloads/ocean-obs",
        help="Root directory to scan recursively for .bufr/.bufr4 files.",
    )
    parser.add_argument(
        "--output",
        default="ocean_obs_readings.csv",
        help="Output CSV path.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="If >0, process only the first N BUFR files (useful for testing).",
    )
    parser.add_argument(
        "--max-vars",
        type=int,
        default=0,
        help="If >0, cap the number of variable names recorded per file.",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser()
    if not root.exists():
        print(f"Root path not found: {root}", file=sys.stderr)
        return 2

    if not _eccodes_available():
        print(
            "Python package 'eccodes' is required to decode BUFR for CSV export.\n"
            "Install it inside your venv, e.g.:\n"
            "  ./vwis2/bin/pip install eccodes\n",
            file=sys.stderr,
        )
        return 2

    out_path = Path(args.output).expanduser()
    max_vars = args.max_vars if args.max_vars > 0 else None

    files = sorted(_iter_bufr_files(root))
    if args.limit and args.limit > 0:
        files = files[: args.limit]

    if not files:
        print(f"No .bufr/.bufr4 files found under: {root}", file=sys.stderr)
        return 2

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["lat", "lon", "variables"])

        for idx, bufr_path in enumerate(files, start=1):
            try:
                lat, lon, variables = _extract_lat_lon_and_vars(bufr_path, max_vars=max_vars)
                writer.writerow(
                    [
                        "" if lat is None else lat,
                        "" if lon is None else lon,
                        ";".join(variables),
                    ]
                )
            except Exception as e:
                writer.writerow(["", "", f"ERROR: {bufr_path}: {e}"])

            if idx % 200 == 0:
                print(f"Processed {idx}/{len(files)} files...", file=sys.stderr)

    print(f"Wrote CSV: {out_path} ({len(files)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
