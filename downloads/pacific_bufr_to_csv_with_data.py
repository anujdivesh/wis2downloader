#!/usr/bin/env python3

from __future__ import annotations

import argparse
import contextlib
import csv
import itertools
import json
import os
import subprocess
import sys
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
    # Lat/Lon
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
    # Only BUFR files (ignore any .csv/.txt/etc)
    for pattern in ("*.bufr", "*.bufr4"):
        yield from root.rglob(pattern)


def _safe_is_missing(eccodes, h, key: str) -> bool:
    try:
        return bool(eccodes.codes_is_missing(h, key))
    except Exception:
        return True


def _safe_get_first_value(
    eccodes,
    h,
    key: str,
    *,
    max_array_scan: int = 64,
    max_array_size: int = 128,
):
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
    if max_array_size > 0 and size > max_array_size:
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
    max_array_size: int,
    max_subsets: int,
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
                    # Read subset metadata without unpacking (cheap).
                    try:
                        number_of_subsets = int(eccodes.codes_get(h, "numberOfSubsets"))
                    except Exception:
                        number_of_subsets = None
                    try:
                        compressed_data = int(eccodes.codes_get(h, "compressedData"))
                    except Exception:
                        compressed_data = None

                    # Safety: avoid unpacking massive messages which can OOM.
                    # If we skip unpacking, lat/lon will likely remain None and the file will be skipped.
                    if number_of_subsets is not None and max_subsets > 0:
                        if number_of_subsets > max_subsets:
                            break

                    # Compressed BUFR with many subsets is especially memory-heavy.
                    if (
                        compressed_data is not None
                        and compressed_data == 1
                        and number_of_subsets is not None
                        and max_subsets > 0
                        and number_of_subsets > max_subsets
                    ):
                        break

                    eccodes.codes_set(h, "unpack", 1)
                    try:
                        eccodes.codes_set(h, "skipExtraKeyAttributes", 1)
                    except Exception:
                        pass

                    if lat is None:
                        for k in LAT_KEYS:
                            v = _safe_get_first_value(eccodes, h, k, max_array_size=16)
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
                            v = _safe_get_first_value(eccodes, h, k, max_array_size=16)
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

                            val = _safe_get_first_value(
                                eccodes,
                                h,
                                key,
                                max_array_size=max_array_size,
                            )
                            if val is None:
                                continue

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


def _worker_emit_records(
    paths: list[Path],
    *,
    max_vars: int | None,
    max_array_size: int,
    max_subsets: int,
    max_file_bytes: int,
) -> int:
    """Worker mode: decode each path and emit one NDJSON line per file to stdout."""

    for bufr_path in paths:
        try:
            if max_file_bytes and max_file_bytes > 0:
                try:
                    if bufr_path.stat().st_size > max_file_bytes:
                        print(
                            json.dumps(
                                {
                                    "path": str(bufr_path),
                                    "skipped": "large_file",
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                        continue
                except Exception:
                    pass

            lat, lon, variables, values = _extract_lat_lon_vars_and_data(
                bufr_path,
                max_vars=max_vars,
                max_array_size=max_array_size,
                max_subsets=max_subsets,
            )

            print(
                json.dumps(
                    {
                        "path": str(bufr_path),
                        "lat": lat,
                        "lon": lon,
                        "variables": variables,
                        "values": values,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        except Exception as e:
            print(
                json.dumps(
                    {
                        "path": str(bufr_path),
                        "error": str(e),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    return 0


def _run_worker_batch_subprocess(
    script_path: Path,
    paths: list[Path],
    *,
    max_vars: int | None,
    max_array_size: int,
    max_subsets: int,
    max_file_bytes: int,
) -> Iterable[dict]:
    """Run this script in worker mode and yield decoded dicts (one per file)."""

    cmd = [
        sys.executable,
        "-u",
        str(script_path),
        "--_worker",
        "--max-array-size",
        str(max_array_size),
        "--max-subsets",
        str(max_subsets),
        "--max-file-bytes",
        str(max_file_bytes),
    ]
    if max_vars is not None:
        cmd += ["--max-vars", str(max_vars)]
    else:
        cmd += ["--max-vars", "0"]

    cmd += [str(p) for p in paths]

    # Inherit stderr so you still see per-file decoding errors.
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
    )
    assert proc.stdout is not None

    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except Exception:
            # Ignore malformed worker output.
            continue

    proc.stdout.close()
    rc = proc.wait()
    yield {"__worker_exit_code__": rc}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Loop through BUFR files under pacific-obs and create a CSV with: lat, lon, variables, data. "
            "Skips files where lat/lon cannot be decoded."
        )
    )
    parser.add_argument(
        "--root",
        default="/Users/anujdivesh/Desktop/wis2.0/final/downloads/pacific-obs",
        help="Root directory to scan recursively for .bufr/.bufr4 files.",
    )
    parser.add_argument(
        "--output",
        default="pacific_obs_readings_with_data.csv",
        help="Output CSV path.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="If >0, process only the first N BUFR files (useful for testing).",
    )
    parser.add_argument(
        "--subprocess",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Decode files in short-lived subprocess worker batches (much more stable for large runs). "
            "Disable with --no-subprocess if you want maximum speed and your system has plenty of RAM."
        ),
    )
    parser.add_argument(
        "--worker-batch-size",
        type=int,
        default=25,
        help="Number of BUFR files handled per worker subprocess (only when --subprocess).",
    )
    parser.add_argument(
        "--max-vars",
        type=int,
        default=0,
        help="If >0, cap the number of variables recorded per file.",
    )
    parser.add_argument(
        "--max-array-size",
        type=int,
        default=128,
        help=(
            "Skip keys whose value is an array larger than this size (prevents OOM). "
            "Set to 0 to disable the check (not recommended)."
        ),
    )
    parser.add_argument(
        "--max-subsets",
        type=int,
        default=2000,
        help=(
            "Do not unpack BUFR messages with more than this many subsets (prevents OOM). "
            "Lower this if the process is still killed; set to 0 to disable (not recommended)."
        ),
    )
    parser.add_argument(
        "--max-file-bytes",
        type=int,
        default=0,
        help=(
            "If >0, skip BUFR files larger than this many bytes (extra safety against huge inputs)."
        ),
    )
    parser.add_argument(
        "--_worker",
        action="store_true",
        default=False,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "_paths",
        nargs="*",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)

    # Worker mode: called by the parent process to avoid long-run ecCodes OOM.
    if args._worker:
        paths = [Path(p) for p in args._paths]
        max_vars = args.max_vars if args.max_vars > 0 else None
        return _worker_emit_records(
            paths,
            max_vars=max_vars,
            max_array_size=args.max_array_size,
            max_subsets=args.max_subsets,
            max_file_bytes=args.max_file_bytes,
        )

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

    file_iter = _iter_bufr_files(root)
    if args.limit and args.limit > 0:
        file_iter = itertools.islice(file_iter, args.limit)

    try:
        first = next(file_iter)
    except StopIteration:
        print(f"No .bufr/.bufr4 files found under: {root}", file=sys.stderr)
        return 2

    file_iter = itertools.chain([first], file_iter)

    written = 0
    skipped_no_latlon = 0
    processed = 0
    skipped_large_file = 0

    script_path = Path(__file__).resolve()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["lat", "lon", "variables", "data"])

        if args.subprocess:
            batch_size = max(1, int(args.worker_batch_size))
            while True:
                batch = list(itertools.islice(file_iter, batch_size))
                if not batch:
                    break

                done_paths: set[str] = set()
                worker_rc = 0

                for rec in _run_worker_batch_subprocess(
                    script_path,
                    batch,
                    max_vars=max_vars,
                    max_array_size=args.max_array_size,
                    max_subsets=args.max_subsets,
                    max_file_bytes=args.max_file_bytes,
                ):
                    if "__worker_exit_code__" in rec:
                        try:
                            worker_rc = int(rec["__worker_exit_code__"])
                        except Exception:
                            worker_rc = 1
                        continue

                    if "error" in rec and rec.get("path"):
                        print(f"ERROR: {rec['path']}: {rec['error']}", file=sys.stderr)
                        done_paths.add(str(rec.get("path")))
                        continue
                    if rec.get("skipped") == "large_file":
                        skipped_large_file += 1
                        done_paths.add(str(rec.get("path")))
                        continue
                    if "error" in rec and not rec.get("path"):
                        print(f"ERROR: {rec['error']}", file=sys.stderr)
                        continue

                    if rec.get("path"):
                        done_paths.add(str(rec.get("path")))

                    lat = rec.get("lat")
                    lon = rec.get("lon")
                    if lat is None or lon is None:
                        skipped_no_latlon += 1
                        continue

                    variables = rec.get("variables") or []
                    values = rec.get("values") or {}
                    writer.writerow(
                        [
                            lat,
                            lon,
                            ";".join(variables),
                            json.dumps(values, ensure_ascii=False, sort_keys=True),
                        ]
                    )
                    written += 1

                if worker_rc != 0:
                    print(
                        f"WARNING: worker exited with code {worker_rc}; retrying remaining files in batch one-by-one...",
                        file=sys.stderr,
                    )
                    remaining = [p for p in batch if str(p) not in done_paths]
                    for p in remaining:
                        # Retry single-file worker to avoid losing rows.
                        single_done = False
                        single_rc = 0
                        for rec in _run_worker_batch_subprocess(
                            script_path,
                            [p],
                            max_vars=max_vars,
                            max_array_size=args.max_array_size,
                            max_subsets=args.max_subsets,
                            max_file_bytes=args.max_file_bytes,
                        ):
                            if "__worker_exit_code__" in rec:
                                try:
                                    single_rc = int(rec["__worker_exit_code__"])
                                except Exception:
                                    single_rc = 1
                                continue

                            single_done = True
                            if "error" in rec and rec.get("path"):
                                print(
                                    f"ERROR: {rec['path']}: {rec['error']}",
                                    file=sys.stderr,
                                )
                                continue
                            if rec.get("skipped") == "large_file":
                                skipped_large_file += 1
                                continue

                            lat = rec.get("lat")
                            lon = rec.get("lon")
                            if lat is None or lon is None:
                                skipped_no_latlon += 1
                                continue

                            variables = rec.get("variables") or []
                            values = rec.get("values") or {}
                            writer.writerow(
                                [
                                    lat,
                                    lon,
                                    ";".join(variables),
                                    json.dumps(
                                        values,
                                        ensure_ascii=False,
                                        sort_keys=True,
                                    ),
                                ]
                            )
                            written += 1

                        if not single_done or single_rc != 0:
                            print(
                                f"ERROR: {p}: worker failed (exit={single_rc})",
                                file=sys.stderr,
                            )

                processed += len(batch)
                if processed % 500 == 0:
                    print(f"Processed {processed} BUFR files...", file=sys.stderr)
                f.flush()
        else:
            for bufr_path in file_iter:
                processed += 1

                if args.max_file_bytes and args.max_file_bytes > 0:
                    try:
                        if bufr_path.stat().st_size > args.max_file_bytes:
                            skipped_large_file += 1
                            continue
                    except Exception:
                        pass
                try:
                    lat, lon, variables, values = _extract_lat_lon_vars_and_data(
                        bufr_path,
                        max_vars=max_vars,
                        max_array_size=args.max_array_size,
                        max_subsets=args.max_subsets,
                    )

                    # Requirement: if BUFR doesn't have lat/lon, don't add it to CSV
                    if lat is None or lon is None:
                        skipped_no_latlon += 1
                        continue

                    writer.writerow(
                        [
                            lat,
                            lon,
                            ";".join(variables),
                            json.dumps(values, ensure_ascii=False, sort_keys=True),
                        ]
                    )
                    written += 1
                except Exception as e:
                    # Keep going; report to stderr.
                    print(f"ERROR: {bufr_path}: {e}", file=sys.stderr)

                if processed % 500 == 0:
                    print(f"Processed {processed} BUFR files...", file=sys.stderr)

    print(
        f"Wrote CSV: {out_path} (rows={written}, skipped_no_latlon={skipped_no_latlon}, skipped_large_file={skipped_large_file}, files_seen={processed})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
