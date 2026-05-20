#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path
from typing import Iterable

import contextlib


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
    for pattern in ("*.bufr", "*.bufr4"):
        yield from root.rglob(pattern)


def _safe_is_missing(eccodes, h, key: str) -> bool:
    try:
        return bool(eccodes.codes_is_missing(h, key))
    except Exception:
        return True


def _safe_get_first_value(eccodes, h, key: str, *, max_array_scan: int = 64):
    """Return a JSON-serializable representative value for key.

    - For scalar keys, returns the scalar.
    - For array keys, returns the first non-missing element (scanning up to max_array_scan).
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

    # Avoid materializing huge arrays (can blow up memory on compressed BUFR).
    # This exporter only needs a representative value; for large arrays we skip.
    if size > 128:
        return None

    try:
        arr = eccodes.codes_get_array(h, key)
    except Exception:
        return None

    if arr is None:
        return None

    scanned = 0
    for val in arr:
        scanned += 1
        if scanned > max_array_scan:
            break
        if val is None:
            continue
        # Drop NaN
        if isinstance(val, float) and val != val:
            continue
        return val

    return None


def _maybe_float(val):
    try:
        return float(val)
    except Exception:
        return None


def _extract_lat_lon_vars_and_data(
    path: Path,
    *,
    max_vars: int | None,
) -> tuple[float | None, float | None, list[str], dict[str, object]]:
    import eccodes

    lat = None
    lon = None

    values: dict[str, object] = {}

    with _suppress_c_stderr(True):
        with path.open("rb") as f:
            while True:
                h = eccodes.codes_bufr_new_from_file(f)
                if h is None:
                    break

                try:
                    eccodes.codes_set(h, "unpack", 1)
                    # Reduce extra attribute lookups that can be noisy on some templates.
                    try:
                        eccodes.codes_set(h, "skipExtraKeyAttributes", 1)
                    except Exception:
                        pass

                    if lat is None:
                        for k in LAT_KEYS:
                            v = _safe_get_first_value(eccodes, h, k)
                            if v is None:
                                continue
                            vv = _maybe_float(v)
                            if vv is None:
                                continue
                            if -90.0 <= vv <= 90.0:
                                lat = vv
                                break

                    if lon is None:
                        for k in LON_KEYS:
                            v = _safe_get_first_value(eccodes, h, k)
                            if v is None:
                                continue
                            vv = _maybe_float(v)
                            if vv is None:
                                continue
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

                            # Avoid ecCodes sequence/hash metadata keys that can be noisy.
                            lk = key.lower()
                            if "sequence" in lk or lk == "sequences" or lk.endswith("hash"):
                                continue
                            if key.startswith("section"):
                                continue
                            if key.startswith("typical"):
                                continue

                            if key in values:
                                continue

                            if _safe_is_missing(eccodes, h, key):
                                continue

                            val = _safe_get_first_value(eccodes, h, key)
                            if val is None:
                                continue

                            # Ensure JSON-serializable types.
                            if isinstance(val, (bytes, bytearray)):
                                val = val.decode("utf-8", errors="replace")
                            elif not isinstance(val, (str, int, float, bool)):
                                val = str(val)

                            values[key] = val
                            if max_vars is not None and len(values) >= max_vars:
                                break
                    finally:
                        eccodes.codes_keys_iterator_delete(it)
                finally:
                    eccodes.codes_release(h)

                if max_vars is not None and len(values) >= max_vars:
                    break

    variables = sorted(values.keys())
    return lat, lon, variables, values


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Loop through BUFR files under a root path and create one CSV row per file: "
            "lat, lon, variables, data. 'data' is JSON with actual decoded values."
        )
    )
    parser.add_argument(
        "--root",
        default="/Users/anujdivesh/Desktop/wis2.0/final/downloads/ocean-obs",
        help="Root directory to scan recursively for .bufr/.bufr4 files.",
    )
    parser.add_argument(
        "--output",
        default="ocean_obs_readings_with_data.csv",
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
        help="If >0, cap the number of variables recorded per file.",
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
        writer.writerow(["lat", "lon", "variables", "data"])

        for idx, bufr_path in enumerate(files, start=1):
            try:
                lat, lon, variables, values = _extract_lat_lon_vars_and_data(
                    bufr_path,
                    max_vars=max_vars,
                )
                writer.writerow(
                    [
                        "" if lat is None else lat,
                        "" if lon is None else lon,
                        ";".join(variables),
                        json.dumps(values, ensure_ascii=False, sort_keys=True),
                    ]
                )
            except Exception as e:
                writer.writerow(["", "", "", f"ERROR: {bufr_path}: {e}"])

            if idx % 200 == 0:
                print(f"Processed {idx}/{len(files)} files...", file=sys.stderr)

    print(f"Wrote CSV: {out_path} ({len(files)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
