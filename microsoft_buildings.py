# -*- coding: utf-8 -*-
"""Microsoft Global ML Building Footprints source for UrbanMorphoBox.

Official data: https://github.com/microsoft/GlobalMLBuildingFootprints
Source data license: CDLA Permissive 2.0.

All IO/GDAL operations here are independent of PyQGIS and may execute inside
QgsTask.run(). Do not access QGIS UI or QgsProject objects from these functions.

City-scale workflow:
  index -> 2 parallel/resumable HTTP downloads -> clipped, streamed GPKG
  -> tile-level transaction checkpoints -> spatial index -> reusable output.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import threading
import time
import zlib
from urllib.parse import urlparse

import requests

DATASET_RELEASE = "2026-07-24"
INDEX_URL = (
    "https://bfppub.blob.core.windows.net/%24web/"
    + DATASET_RELEASE + "/dataset-links.csv"
)
ZOOM_LEVEL = 9
MAX_MERCATOR_LAT = 85.05112878

# Guardrails for CITY downloads; do not just remove limits.
MAX_QUADKEYS = 64
MAX_LINKS = 96
MAX_COMPRESSED_BYTES = 2 * 1024 ** 3   # 2 GiB/file
MAX_TOTAL_COMPRESSED_BYTES = 8 * 1024 ** 3  # 8 GiB/job
MAX_OUTPUT_FEATURES = 5_000_000
PARALLEL_DOWNLOADS = 2
MIN_FREE_DISK_BYTES = 512 * 1024 ** 2
INDEX_CACHE_TTL_SECONDS = 7 * 24 * 3600
CHUNK_SIZE = 1024 * 1024
MAX_RETRIES = 3
RETRY_DELAYS_SECONDS = (2, 5)
CHECKPOINT_EVERY_LINES = 10_000


class Cancelled(Exception):
    """User canceled the job; partial downloads and completed tiles are kept."""


class DownloadLimitError(RuntimeError):
    """Configured safety limit or disk-space threshold would be exceeded."""


def _check_cancel(cancelled):
    if cancelled is not None and cancelled():
        raise Cancelled("Microsoft building download canceled")


def _wait_or_cancel(seconds, cancelled):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        _check_cancel(cancelled)
        time.sleep(min(0.15, end - time.monotonic()))


def _tile_x(lon, zoom):
    n = 1 << zoom
    return max(0, min(n - 1, math.floor((lon + 180) / 360 * n)))


def _tile_y(lat, zoom):
    lat = max(-MAX_MERCATOR_LAT, min(MAX_MERCATOR_LAT, lat))
    n = 1 << zoom
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) * n / 2
    return max(0, min(n - 1, math.floor(y)))


def _quadkey(x, y, zoom):
    result = []
    for level in range(zoom, 0, -1):
        mask = 1 << (level - 1)
        result.append(str(int(bool(x & mask)) + 2 * int(bool(y & mask))))
    return "".join(result)


def quadkeys_for_bbox(bbox, zoom=ZOOM_LEVEL):
    """All zoom-level quadkeys intersecting WGS84 (west, south, east, north)."""
    west, south, east, north = map(float, bbox)
    if not all(map(math.isfinite, (west, south, east, north))):
        raise ValueError("Non-finite study area coordinates")
    if not (-180 <= west < east <= 180):
        raise ValueError("Study area must have -180 <= west < east <= 180")
    if not (-MAX_MERCATOR_LAT <= south < north <= MAX_MERCATOR_LAT):
        raise ValueError("Study area is outside supported Web Mercator latitudes")
    x0 = _tile_x(west, zoom)
    x1 = _tile_x(math.nextafter(east, -math.inf), zoom)
    y0 = _tile_y(north, zoom)
    y1 = _tile_y(math.nextafter(south, math.inf), zoom)
    tile_count = (x1 - x0 + 1) * (y1 - y0 + 1)
    # Check BEFORE materializing an unbounded set.
    if tile_count > MAX_QUADKEYS:
        raise DownloadLimitError(
            f"The study area covers {tile_count} Microsoft tiles "
            f"(safety limit: {MAX_QUADKEYS}). Choose a smaller area."
        )
    return {
        _quadkey(x, y, zoom)
        for x in range(x0, x1 + 1)
        for y in range(y0, y1 + 1)
    }


def _trusted_url(url):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and (host.endswith(".blob.core.windows.net")
             or host.endswith(".web.core.windows.net"))
        and not parsed.username and not parsed.password
    )


def _index_bytes(cache_dir, cancelled=None):
    _check_cancel(cancelled)
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"dataset-links-{DATASET_RELEASE}.csv"
    if (path.exists() and path.stat().st_size
            and time.time() - path.stat().st_mtime < INDEX_CACHE_TTL_SECONDS):
        return path.read_bytes()
    with requests.get(
        INDEX_URL, timeout=(15, 60), stream=True,
        headers={"User-Agent": "UrbanMorphoBox/0.3-city (tile index)"},
    ) as response:
        response.raise_for_status()
        data = bytearray()
        for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
            _check_cancel(cancelled)
            data.extend(chunk)
            if len(data) > 20 * 1024 * 1024:
                raise DownloadLimitError("Microsoft tile index exceeds 20 MiB")
    _check_cancel(cancelled)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return bytes(data)


def find_microsoft_tiles(bbox, cache_dir, cancelled=None):
    """Return matching {quadkey, location, url}, without downloading footprints."""
    keys = quadkeys_for_bbox(bbox)
    raw = _index_bytes(cache_dir, cancelled)
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    fields = {name.strip().lower(): name for name in (reader.fieldnames or [])}
    if not all(name in fields for name in ("quadkey", "url", "location")):
        raise ValueError("Microsoft index must contain QuadKey, Url and Location")
    matches, seen = [], set()
    for row in reader:
        _check_cancel(cancelled)
        key = (row.get(fields["quadkey"]) or "").strip()
        if key not in keys:
            continue
        url = (row.get(fields["url"]) or "").strip()
        if not _trusted_url(url):
            raise ValueError(f"Unexpected Microsoft download URL for tile {key}")
        identity = (key, url)
        if identity in seen:
            continue
        seen.add(identity)
        matches.append({
            "quadkey": key,
            "location": (row.get(fields["location"]) or "").strip(),
            "url": url,
        })
        if len(matches) > MAX_LINKS:
            raise DownloadLimitError(f"More than {MAX_LINKS} download files; smaller area needed")
    return sorted(matches, key=lambda d: (d["quadkey"], d["location"], d["url"]))


def _cache_path(cache_dir, match):
    digest = hashlib.sha256(match["url"].encode()).hexdigest()[:16]
    return Path(cache_dir) / "tiles" / f"{match['quadkey']}-{digest}.csv.gz"


def cache_status(matches, cache_dir):
    """Number and size of existing completed tiles; safe to call from GUI."""
    cached, bytes_cached = 0, 0
    for match in matches:
        path = _cache_path(cache_dir, match)
        if path.exists() and 0 < path.stat().st_size <= MAX_COMPRESSED_BYTES:
            with path.open("rb") as stream:
                if stream.read(2) == b"\x1f\x8b":
                    cached += 1
                    bytes_cached += path.stat().st_size
    return cached, bytes_cached


def _enough_disk(path, additional_bytes=0):
    free = shutil.disk_usage(path).free
    if free < MIN_FREE_DISK_BYTES + additional_bytes:
        raise DownloadLimitError(
            "Not enough free disk space for Microsoft downloads/output "
            f"({free / 1024**3:.1f} GiB available)."
        )


def _safe_range_header(response, position):
    # An HTTP 206 response is safe to append only if the advertised start
    # exactly matches the number of bytes already stored.
    raw = response.headers.get("Content-Range", "")
    match = re.match(r"^bytes (\d+)-(\d+)/(\d+|\*)$", raw)
    return bool(match and int(match.group(1)) == position)


def _validate_gzip(path, cancelled=None):
    """Validate gzip CRC/trailer, so a truncated tile never enters the cache."""
    with gzip.open(path, "rb") as gz:
        while gz.read(CHUNK_SIZE):
            _check_cancel(cancelled)


def download_tile(match, cache_dir, cancelled=None, report=None, bytes_report=None):
    """Download one tile, resuming .part with HTTP Range when supported.

    Returns (path, was_cached). Failed attempts preserve .part to resume later;
    completed downloads are CRC-checked and atomically renamed to .csv.gz.
    """
    if not _trusted_url(match["url"]):
        raise ValueError("Untrusted Microsoft tile URL")
    path = _cache_path(cache_dir, match)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.stat().st_size > MAX_COMPRESSED_BYTES:
            raise DownloadLimitError("Cached tile exceeds per-file safety limit")
        with path.open("rb") as handle:
            if handle.read(2) == b"\x1f\x8b":
                if bytes_report:
                    bytes_report(path.stat().st_size)
                if report:
                    report(1.0)
                return path, True
        path.unlink()

    partial = path.with_name(path.name + ".part")
    for attempt in range(MAX_RETRIES):
        _check_cancel(cancelled)
        already = partial.stat().st_size if partial.exists() else 0
        if bytes_report:
            bytes_report(already)
        if already > MAX_COMPRESSED_BYTES:
            raise DownloadLimitError("Partial tile exceeds per-file limit")
        headers = {"User-Agent": "UrbanMorphoBox/0.3-city (footprints)"}
        if already:
            headers["Range"] = f"bytes={already}-"
        try:
            _enough_disk(path.parent)
            with requests.get(
                match["url"], timeout=(15, 60), stream=True,
                headers=headers,
            ) as response:
                if response.status_code == 416 and already:
                    partial.unlink(missing_ok=True)
                    continue
                response.raise_for_status()
                if response.status_code == 206:
                    if not already or not _safe_range_header(response, already):
                        raise ValueError("Invalid HTTP Range response; cannot safely resume")
                    mode = "ab"
                elif response.status_code == 200:
                    mode = "wb"  # Server ignored Range; restart from zero.
                    already = 0
                else:
                    raise ValueError(f"Unexpected HTTP status {response.status_code}")
                length_str = response.headers.get("Content-Length", "")
                remaining = int(length_str) if length_str.isdigit() else 0
                expected = already + remaining if remaining else 0
                if expected > MAX_COMPRESSED_BYTES:
                    raise DownloadLimitError("Tile exceeds per-file compressed limit (2 GiB)")
                if expected:
                    _enough_disk(path.parent, max(0, expected - already))
                received = already
                with partial.open(mode) as handle:
                    for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                        _check_cancel(cancelled)
                        if not chunk:
                            continue
                        received += len(chunk)
                        if received > MAX_COMPRESSED_BYTES:
                            raise DownloadLimitError("Tile exceeds per-file limit")
                        if bytes_report:
                            bytes_report(received)  # Global job budget, before writing.
                        handle.write(chunk)
                        if report and expected:
                            report(min(1.0, received / expected))
            _check_cancel(cancelled)
            if partial.stat().st_size < 10:
                raise ValueError("Empty Microsoft tile response")
            with partial.open("rb") as handle:
                if handle.read(2) != b"\x1f\x8b":
                    raise ValueError("Tile is not gzip-compressed")
            # Ensure a truncated/invalid gzip is never committed as cached.
            _validate_gzip(partial, cancelled)
            os.replace(partial, path)
            if report:
                report(1.0)
            return path, False
        except Cancelled:
            raise  # Keep partial: next run can resume.
        except DownloadLimitError:
            raise
        except (requests.RequestException, OSError, EOFError, gzip.BadGzipFile, zlib.error, ValueError) as error:
            if isinstance(error, requests.HTTPError):
                status = error.response.status_code if error.response is not None else None
                if status not in (429, 500, 502, 503, 504):
                    raise
            # Corrupt complete gzip cannot be resumed; start from scratch.
            if isinstance(error, (gzip.BadGzipFile, zlib.error, EOFError, ValueError)):
                partial.unlink(missing_ok=True)
            if attempt == MAX_RETRIES - 1:
                raise RuntimeError(
                    f"Tile {match['quadkey']} failed after {MAX_RETRIES} attempts: {error}"
                ) from error
            _wait_or_cancel(RETRY_DELAYS_SECONDS[min(attempt, 1)], cancelled)
    raise RuntimeError(f"Could not download tile {match['quadkey']}")


def _job_key(matches, bbox):
    """Stable ID so an interrupted geometry job can resume next QGIS session."""
    payload = {
        "release": DATASET_RELEASE,
        "bbox": [round(float(x), 9) for x in bbox],
        "tiles": sorted((m["quadkey"], m["url"]) for m in matches),
        "schema": 3,
    }
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()[:18]


def _geometry_envelope(geometry):
    """Fast bbox from Polygon/MultiPolygon coordinates, before GDAL allocation."""
    kind = geometry.get("type")
    coords = geometry.get("coordinates")
    if not isinstance(coords, list):
        return None
    polygons = [coords] if kind == "Polygon" else coords if kind == "MultiPolygon" else []
    xmin = ymin = math.inf
    xmax = ymax = -math.inf
    seen = False
    try:
        for polygon in polygons:
            for ring in polygon:
                for xy in ring:
                    x, y = float(xy[0]), float(xy[1])
                    if not math.isfinite(x) or not math.isfinite(y):
                        return None
                    xmin, xmax = min(xmin, x), max(xmax, x)
                    ymin, ymax = min(ymin, y), max(ymax, y)
                    seen = True
    except (TypeError, ValueError, IndexError):
        return None
    return (xmin, ymin, xmax, ymax) if seen else None


def _bbox_overlaps(env, bbox):
    a, b, c, d = env
    w, s, e, n = bbox
    return a <= e and c >= w and b <= n and d >= s


def _bbox_inside(env, bbox):
    a, b, c, d = env
    w, s, e, n = bbox
    return w <= a and c <= e and s <= b and d <= n


def _optional_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number != -1.0 else None


def _extent_geom(ogr, bbox):
    west, south, east, north = bbox
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in ((west, south), (east, south), (east, north),
                 (west, north), (west, south)):
        ring.AddPoint_2D(x, y)
    geom = ogr.Geometry(ogr.wkbPolygon)
    geom.AddGeometry(ring)
    return geom


def _polygonal_only(ogr, geom):
    """Extract polygons after clipping (may produce GeometryCollection)."""
    if geom is None or geom.IsEmpty():
        return None
    name = geom.GetGeometryName().upper()
    if name == "MULTIPOLYGON":
        return geom
    if name == "POLYGON":
        return ogr.ForceToMultiPolygon(geom)
    if name != "GEOMETRYCOLLECTION":
        return None
    out = ogr.Geometry(ogr.wkbMultiPolygon)
    for i in range(geom.GetGeometryCount()):
        part = _polygonal_only(ogr, geom.GetGeometryRef(i))
        if part is None:
            continue
        for j in range(part.GetGeometryCount()):
            out.AddGeometry(part.GetGeometryRef(j))
    return out if out.GetGeometryCount() else None


def _create_work_gpkg(ogr, osr, path):
    """Create GeoPackage with polygon features and transactional checkpoints."""
    driver = ogr.GetDriverByName("GPKG")
    if driver is None:
        raise RuntimeError("GDAL GeoPackage driver is unavailable")
    ds = driver.CreateDataSource(str(path))
    if ds is None:
        raise RuntimeError("Cannot create GeoPackage work file")
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    layer = ds.CreateLayer(
        "microsoft_buildings", srs=srs, geom_type=ogr.wkbMultiPolygon,
        options=["SPATIAL_INDEX=NO"],
    )
    if layer is None:
        raise RuntimeError("Cannot create Microsoft buildings layer")
    for name, kind in (
        ("source", ogr.OFTString),
        ("quadkey", ogr.OFTString),
        ("release", ogr.OFTString),
        ("area_m2", ogr.OFTReal),
        ("height_m", ogr.OFTReal),
        ("confidence", ogr.OFTReal),
    ):
        if layer.CreateField(ogr.FieldDefn(name, kind)) != 0:
            raise RuntimeError(f"Failed to create field {name}")
    status = ds.CreateLayer("umb_completed_tiles", geom_type=ogr.wkbNone)
    if status is None:
        raise RuntimeError("Cannot create progress/checkpoint layer")
    for field, kind in (
        ("tile_key", ogr.OFTString),
        ("last_line", ogr.OFTInteger64),
        ("complete", ogr.OFTInteger),
        ("features", ogr.OFTInteger64),
        ("invalid", ogr.OFTInteger64),
        ("lines", ogr.OFTInteger64),
        ("area_m2", ogr.OFTReal),
    ):
        if status.CreateField(ogr.FieldDefn(field, kind)) != 0:
            raise RuntimeError(f"Cannot create checkpoint field {field}")
    return ds


def _load_checkpoints(status):
    """Retrieve checkpoint rows for safe restart at each downloaded tile."""
    result = {}
    status.ResetReading()
    for feature in status:
        key = feature.GetField("tile_key")
        if not key or key in result:
            raise RuntimeError("Invalid or duplicate tile checkpoint in work GeoPackage")
        result[key] = {
            "fid": feature.GetFID(),
            "last_line": int(feature.GetField("last_line") or 0),
            "complete": bool(feature.GetField("complete")),
            **{
                name: feature.GetField(name) or 0
                for name in ("features", "invalid", "lines", "area_m2")
            },
        }
    return result


def _save_checkpoint(ogr, status, existing, tile_key, last_line,
                     complete, tile_stats):
    """Write checkpoint into SAME GDAL transaction as feature inserts."""
    if existing is not None:
        marker = status.GetFeature(existing["fid"])
        if marker is None:
            raise RuntimeError("Missing previously saved tile checkpoint")
    else:
        marker = ogr.Feature(status.GetLayerDefn())
    marker.SetField("tile_key", tile_key)
    marker.SetField("last_line", int(last_line))
    marker.SetField("complete", int(bool(complete)))
    for name in ("features", "invalid", "lines", "area_m2"):
        marker.SetField(name, tile_stats[name])
    if existing is None:
        if status.CreateFeature(marker) != 0:
            raise RuntimeError("Could not create tile checkpoint")
    elif status.SetFeature(marker) != 0:
        raise RuntimeError("Could not update tile checkpoint")
    return {"fid": marker.GetFID(), "last_line": int(last_line),
            "complete": bool(complete), **tile_stats}


def _build_output(work_path, matches, bbox, input_paths, cancelled=None, report=None):
    """Create/resume a city GPKG with 10k-line atomic checkpoints.

    For each tile, both footprints AND the last processed input-line number
    are committed in the same SQLite transaction. On restart, committed
    lines are decompressed and skipped, without inserting duplicates.
    """
    from osgeo import ogr, osr
    work_path = Path(work_path)
    work_path.parent.mkdir(parents=True, exist_ok=True)
    if work_path.exists():
        ds = ogr.Open(str(work_path), update=1)
        if ds is None:
            raise RuntimeError(f"Cannot resume work GeoPackage: {work_path}")
    else:
        _enough_disk(work_path.parent)
        ds = _create_work_gpkg(ogr, osr, work_path)
    layer = ds.GetLayerByName("microsoft_buildings")
    status = ds.GetLayerByName("umb_completed_tiles")
    if layer is None or status is None:
        ds = None
        raise RuntimeError("Incompatible work GPKG; remove the .work file to restart")
    checkpoints = _load_checkpoints(status)
    current_total = layer.GetFeatureCount()
    if current_total != sum(x["features"] for x in checkpoints.values()):
        raise RuntimeError("Checkpoint/feature mismatch in work GPKG; cannot resume safely")
    if current_total > MAX_OUTPUT_FEATURES:
        raise DownloadLimitError("Work GeoPackage already exceeds the feature limit")

    wgs84 = osr.SpatialReference()
    wgs84.ImportFromEPSG(4326)
    wgs84.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    equal_area = osr.SpatialReference()
    equal_area.ImportFromEPSG(6933)
    equal_area.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    transform = osr.CoordinateTransformation(wgs84, equal_area)
    study_geom = _extent_geom(ogr, bbox)
    stats = {
        "features": current_total,
        "invalid": sum(row["invalid"] for row in checkpoints.values()),
        "lines": sum(row["lines"] for row in checkpoints.values()),
        "area_m2": sum(row["area_m2"] for row in checkpoints.values()),
        "resumed_tiles": len(checkpoints),
        "processed_tiles": sum(x["complete"] for x in checkpoints.values()),
    }
    try:
        for idx, (match, input_path) in enumerate(zip(matches, input_paths)):
            _check_cancel(cancelled)
            tile_id = match["quadkey"] + ":" + hashlib.sha256(
                match["url"].encode()
            ).hexdigest()[:16]
            checkpoint = checkpoints.get(tile_id)
            if checkpoint and checkpoint["complete"]:
                if report:
                    report((idx + 1) / len(matches))
                continue
            skipped_lines = checkpoint["last_line"] if checkpoint else 0
            base = {k: checkpoint[k] if checkpoint else 0
                    for k in ("features", "invalid", "lines", "area_m2")}
            before = {k: stats[k] for k in base}
            transaction_open = False
            try:
                if ds.StartTransaction() != 0:
                    raise RuntimeError("Could not begin GPKG transaction")
                transaction_open = True
                file_size = max(1, input_path.stat().st_size)
                last_line = 0
                with input_path.open("rb") as compressed:
                    with gzip.GzipFile(fileobj=compressed) as stream:
                        for line_no, line in enumerate(stream, start=1):
                            last_line = line_no
                            if line_no <= skipped_lines:
                                # This input line was committed on a previous run.
                                continue
                            stats["lines"] += 1
                            if line_no % 500 == 0:
                                _check_cancel(cancelled)
                                if report:
                                    frac = min(1.0, compressed.tell() / file_size)
                                    report((idx + frac) / len(matches))
                            try:
                                obj = json.loads(line)
                                raw_geom = obj.get("geometry")
                                if not isinstance(raw_geom, dict):
                                    stats["invalid"] += 1
                                    continue
                                env = _geometry_envelope(raw_geom)
                                if env is None:
                                    stats["invalid"] += 1
                                    continue
                                if not _bbox_overlaps(env, bbox):
                                    continue
                                polygon = ogr.CreateGeometryFromJson(json.dumps(raw_geom))
                                if polygon is None or polygon.IsEmpty():
                                    stats["invalid"] += 1
                                    continue
                                if _bbox_inside(env, bbox):
                                    clipped = polygon
                                else:
                                    if not polygon.Intersects(study_geom):
                                        continue
                                    clipped = polygon.Intersection(study_geom)
                                clipped = _polygonal_only(ogr, clipped)
                                if clipped is None or clipped.IsEmpty():
                                    continue
                                metric = clipped.Clone()
                                if metric.Transform(transform) != 0:
                                    stats["invalid"] += 1
                                    continue
                                area = metric.GetArea()
                                if not math.isfinite(area) or area <= 0:
                                    stats["invalid"] += 1
                                    continue
                                if stats["features"] >= MAX_OUTPUT_FEATURES:
                                    raise DownloadLimitError(
                                        f"Output limit ({MAX_OUTPUT_FEATURES:,} buildings) exceeded"
                                    )
                                props = obj.get("properties") or {}
                                output = ogr.Feature(layer.GetLayerDefn())
                                output.SetGeometry(clipped)
                                output.SetField("source", "Microsoft")
                                output.SetField("quadkey", match["quadkey"])
                                output.SetField("release", DATASET_RELEASE)
                                output.SetField("area_m2", float(area))
                                for source_name, field_name in (
                                    ("height", "height_m"), ("confidence", "confidence")
                                ):
                                    value = _optional_float(props.get(source_name))
                                    if value is not None:
                                        output.SetField(field_name, value)
                                if layer.CreateFeature(output) != 0:
                                    raise RuntimeError("GeoPackage feature write failed")
                                output = None
                                stats["features"] += 1
                                stats["area_m2"] += area
                            except (json.JSONDecodeError, TypeError, ValueError, KeyError):
                                stats["invalid"] += 1
                                continue
                            finally:
                                # Commit no matter whether line was inside the bbox.
                                # A "continue" within the try still runs finally.
                                if line_no % CHECKPOINT_EVERY_LINES == 0:
                                    tile_stats = {k: base[k] + stats[k] - before[k]
                                                  for k in base}
                                    checkpoint = _save_checkpoint(
                                        ogr, status, checkpoint, tile_id,
                                        line_no, False, tile_stats,
                                    )
                                    if ds.CommitTransaction() != 0:
                                        raise RuntimeError("Cannot commit GeoPackage batch")
                                    transaction_open = False
                                    _enough_disk(work_path.parent)
                                    if ds.StartTransaction() != 0:
                                        raise RuntimeError("Cannot start next GeoPackage batch")
                                    transaction_open = True
                if last_line < skipped_lines:
                    raise RuntimeError("Tile changed since last run; cannot resume safely")
                tile_stats = {k: base[k] + stats[k] - before[k] for k in base}
                checkpoint = _save_checkpoint(
                    ogr, status, checkpoint, tile_id, last_line, True, tile_stats,
                )
                if ds.CommitTransaction() != 0:
                    raise RuntimeError("Cannot commit completed tile")
                transaction_open = False
                checkpoints[tile_id] = checkpoint
                stats["processed_tiles"] += 1
            except Exception:
                if transaction_open:
                    ds.RollbackTransaction()
                raise
            if report:
                report((idx + 1) / len(matches))
        _check_cancel(cancelled)
        # Index is expensive to maintain per feature; create once at the end.
        answer = ds.ExecuteSQL("SELECT HasSpatialIndex('microsoft_buildings','geom')")
        has_index = False
        if answer is not None:
            row = answer.GetNextFeature()
            has_index = row is not None and bool(row.GetField(0))
            ds.ReleaseResultSet(answer)
        if not has_index:
            answer = ds.ExecuteSQL(
                "SELECT CreateSpatialIndex('microsoft_buildings','geom')"
            )
            if answer is not None:
                ds.ReleaseResultSet(answer)
        return stats
    finally:
        ds = None


def _metadata_path(gpkg_path):
    return Path(gpkg_path).with_suffix(".json")


def _read_result(path, job_key):
    """Reuse finalized identical study area without recomputing."""
    sidecar = _metadata_path(path)
    if not (path.exists() and sidecar.exists()):
        return None
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        if data.get("job_key") != job_key:
            return None
        return dict(data["stats"], path=str(path), reused_output=True)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def download_and_export(matches, bbox, cache_dir, output_dir, cancelled=None, report=None):
    """Concurrent resumable downloads + resumable tile-by-tile GeoPackage.

    QgsTask.setProgress can be passed as report(value) where value is 0..100.
    Retains valid cache files and completed GeoPackage tiles on failure/cancel.
    """
    if not matches:
        raise ValueError("No Microsoft tiles selected")
    if len(matches) > MAX_LINKS:
        raise DownloadLimitError("Too many Microsoft download files")
    bbox = tuple(map(float, bbox))
    quadkeys_for_bbox(bbox)  # Also validates bbox and tile count.
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    job_key = _job_key(matches, bbox)
    final_path = output_dir / f"microsoft_buildings_{job_key}.gpkg"
    finished_result = _read_result(final_path, job_key)
    if finished_result is not None:
        if report:
            report(100.0)
        return finished_result
    if final_path.exists():
        raise RuntimeError(
            f"Output GeoPackage already exists without matching metadata: {final_path}"
        )
    work_path = output_dir / ".work" / f"microsoft_buildings_{job_key}.gpkg"
    work_path.parent.mkdir(parents=True, exist_ok=True)

    # Concurrent network downloads only; all GDAL writing happens in one
    # sequential writer to avoid SQLite conflicts.
    lock = threading.Lock()
    stop = threading.Event()
    fractions = [0.0] * len(matches)
    byte_counts = [0] * len(matches)
    file_paths = [None] * len(matches)
    cache_flags = [False] * len(matches)
    def was_canceled():
        return stop.is_set() or (cancelled is not None and cancelled())
    def transfer_one(index, match):
        def update(fraction):
            with lock:
                fractions[index] = fraction
                percent = 50.0 * sum(fractions) / len(matches)
            if report:
                report(percent)
        def account_bytes(count):
            with lock:
                byte_counts[index] = count
                if sum(byte_counts) > MAX_TOTAL_COMPRESSED_BYTES:
                    stop.set()
                    raise DownloadLimitError(
                        "Microsoft transfer exceeds the 8 GiB job safety limit"
                    )
        return download_tile(
            match, cache_dir, cancelled=was_canceled,
            report=update, bytes_report=account_bytes,
        )

    try:
        with ThreadPoolExecutor(max_workers=min(PARALLEL_DOWNLOADS, len(matches))) as pool:
            futures = {
                pool.submit(transfer_one, idx, match): idx
                for idx, match in enumerate(matches)
            }
            for future in as_completed(futures):
                _check_cancel(was_canceled)
                index = futures[future]
                path, cached = future.result()
                file_paths[index] = path
                cache_flags[index] = cached
    except Exception:
        stop.set()
        raise
    _check_cancel(cancelled)
    total_bytes = sum(path.stat().st_size for path in file_paths)
    if total_bytes > MAX_TOTAL_COMPRESSED_BYTES:
        raise DownloadLimitError(
            f"Total cached/downloaded tiles: {total_bytes / 1024**3:.1f} GiB, "
            f"limit: {MAX_TOTAL_COMPRESSED_BYTES / 1024**3} GiB"
        )

    _enough_disk(work_path.parent)
    def progress_output(fraction):
        if report:
            report(50.0 + 50.0 * fraction)
    stats = _build_output(
        work_path, matches, bbox, file_paths,
        cancelled=cancelled, report=progress_output,
    )
    _check_cancel(cancelled)
    result = {
        "path": str(final_path),
        "download_files": len(matches),
        "cached_files": sum(cache_flags),
        "compressed_bytes": total_bytes,
        "features": stats["features"],
        "area_m2": stats["area_m2"],
        "invalid": stats["invalid"],
        "lines": stats["lines"],
        "resumed_tiles": stats["resumed_tiles"],
        "processed_tiles": stats["processed_tiles"],
    }
    metadata = {
        "source": "Microsoft Global ML Building Footprints",
        "license": "CDLA Permissive 2.0",
        "dataset_release": DATASET_RELEASE,
        "index_url": INDEX_URL,
        "bbox_wgs84": list(bbox),
        "tiles": matches,
        "job_key": job_key,
        "stats": result,
        "area_method": "Clipped to WGS84 bbox; EPSG:6933 equal-area footprint area",
    }
    sidecar = _metadata_path(final_path)
    temporary_json = sidecar.with_name(sidecar.name + ".tmp")
    try:
        temporary_json.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        os.replace(work_path, final_path)
        os.replace(temporary_json, sidecar)
    finally:
        temporary_json.unlink(missing_ok=True)
    if report:
        report(100.0)
    return result
