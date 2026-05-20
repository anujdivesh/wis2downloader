#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import html
import json
import sys
from pathlib import Path


def _normalize_lon_360(lon: float) -> float:
    """Normalize longitude into [0, 360).

    This helps visualizing points across the dateline when your dataset
    includes both positive and negative longitudes.
    """

    if lon < 0.0:
        lon += 360.0
    # Keep in range even if input was outside expected bounds.
    lon = lon % 360.0
    return lon


def _parse_float(value: str) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _popup_html(lat: float, lon: float, variables: list[str], data: dict[str, object]) -> str:
    # 2-row table: row 1 = variable names, row 2 = values
    header_cells = "".join(f"<th>{html.escape(v)}</th>" for v in variables)
    value_cells = "".join(
        f"<td>{html.escape(str(data.get(v, '')))}</td>" for v in variables
    )

    return (
        "<div style='max-width:900px; overflow-x:auto;'>"
        f"<div><b>lat</b>: {lat:.6f} &nbsp; <b>lon</b>: {lon:.6f}</div>"
        "<table border='1' cellpadding='4' cellspacing='0' style='border-collapse:collapse; margin-top:6px;'>"
        f"<tr>{header_cells}</tr>"
        f"<tr>{value_cells}</tr>"
        "</table>"
        "</div>"
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Plot ocean BUFR CSV points on a Folium map. "
            "Markers use lat/lon; popup shows a 2-row table: variables and values from data JSON."
        )
    )
    parser.add_argument(
        "--input",
        default="pacific_obs_readings_with_data.csv",
        help="Input CSV with header: lat,lon,variables,data",
    )
    parser.add_argument(
        "--output",
        default="ocean_obs_map_pacific.html",
        help="Output HTML file to create.",
    )
    args = parser.parse_args(argv)

    in_path = Path(args.input).expanduser()
    if not in_path.exists():
        print(f"Input CSV not found: {in_path}", file=sys.stderr)
        return 2

    try:
        import folium
    except Exception:
        print(
            "Missing dependency: folium\n"
            "Install it in your venv, e.g.:\n"
            "  ./vwis2/bin/pip install folium\n",
            file=sys.stderr,
        )
        return 2

    points: list[tuple[float, float, list[str], dict[str, object]]] = []

    with in_path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        required = {"lat", "lon", "variables", "data"}
        if reader.fieldnames is None or not required.issubset(set(reader.fieldnames)):
            print(
                f"CSV must have columns {sorted(required)}; found: {reader.fieldnames}",
                file=sys.stderr,
            )
            return 2

        for row in reader:
            lat = _parse_float((row.get("lat") or "").strip())
            lon = _parse_float((row.get("lon") or "").strip())
            if lat is None or lon is None:
                continue

            lon = _normalize_lon_360(lon)

            variables_str = (row.get("variables") or "").strip()
            variables = [v for v in variables_str.split(";") if v]

            data_str = (row.get("data") or "").strip()
            data: dict[str, object]
            try:
                parsed = json.loads(data_str) if data_str else {}
                data = parsed if isinstance(parsed, dict) else {"_data": parsed}
            except Exception:
                data = {"_data": data_str}

            points.append((lat, lon, variables, data))

    if not points:
        print("No valid points found (missing lat/lon?)", file=sys.stderr)
        return 2

    center_lat = sum(p[0] for p in points) / len(points)
    center_lon = sum(p[1] for p in points) / len(points)

    m = folium.Map(location=[center_lat, center_lon], zoom_start=2)

    for lat, lon, variables, data in points:
        popup = folium.Popup(_popup_html(lat, lon, variables, data), max_width=1000)
        folium.Marker(location=[lat, lon], popup=popup).add_to(m)

    out_path = Path(args.output).expanduser()
    m.save(str(out_path))
    print(f"Wrote map HTML: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
