"""Offline unit tests for Microsoft tile lookup and caching.

Run inside the plugin folder with: python -m unittest -v test_microsoft_buildings
These tests do not require QGIS. The full GeoPackage workflow requires a QGIS
installation with GDAL Python bindings and is tested interactively in QGIS.
"""
import gzip
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from microsoft_buildings import (
    Cancelled,
    _optional_float,
    _cache_path,
    quadkeys_for_bbox,
    find_microsoft_tiles,
    download_tile,
    DownloadLimitError,
    _geometry_envelope,
    _bbox_overlaps,
    _bbox_inside,
    _job_key,
)


class FakeResponse:
    def __init__(self, contents, headers=None, status_code=200):
        self.content = contents
        self.headers = headers or {}
        self.status_code = status_code

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=None):
        yield self.content

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class TileTests(unittest.TestCase):
    def test_potsdam_quadkey(self):
        self.assertEqual(quadkeys_for_bbox((13.03, 52.38, 13.10, 52.43)), {"120212010"})

    def test_nonzero_zero_crossing(self):
        keys = quadkeys_for_bbox((-0.1, -0.1, 0.1, 0.1))
        self.assertEqual(len(keys), 4)
        self.assertTrue(all(len(key) == 9 for key in keys))

    def test_bbox_validation(self):
        for bbox in ((13, 52, 13, 53), (13, 52, 12, 53), (-181, 0, -180, 1)):
            with self.assertRaises(ValueError):
                quadkeys_for_bbox(bbox)

    def test_lookup_mocked_index(self):
        csv = ("Location,QuadKey,Url\n"
               "Germany,120212010,https://bfppub.blob.core.windows.net/data/foo.csv.gz\n"
               "Germany,120212011,https://bfppub.blob.core.windows.net/data/bar.csv.gz\n")
        with tempfile.TemporaryDirectory() as tmp:
            with patch("microsoft_buildings.requests.get", return_value=FakeResponse(csv.encode())):
                rows = find_microsoft_tiles((13.03, 52.38, 13.10, 52.43), tmp)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["quadkey"], "120212010")
            self.assertEqual(rows[0]["location"], "Germany")

    def test_cached_gzip(self):
        url = "https://bfppub.blob.core.windows.net/data/foo.csv.gz"
        match = {"quadkey": "120212010", "url": url, "location": "Germany"}
        raw = gzip.compress(b'{"type":"Feature","geometry":null}\n')
        with tempfile.TemporaryDirectory() as tmp:
            with patch("microsoft_buildings.requests.get", return_value=FakeResponse(raw, {"Content-Length": str(len(raw))})) as get:
                path, cached = download_tile(match, tmp)
                self.assertFalse(cached)
                self.assertTrue(path.exists())
                path2, cached2 = download_tile(match, tmp)
                self.assertEqual(path, path2)
                self.assertTrue(cached2)
                self.assertEqual(get.call_count, 1)

    def test_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(Cancelled):
                find_microsoft_tiles((13.03, 52.38, 13.10, 52.43), tmp, cancelled=lambda: True)

    def test_city_tile_safety_limit(self):
        with self.assertRaises(DownloadLimitError):
            quadkeys_for_bbox((-180, -80, 180, 80))

    def test_polygon_envelope_and_bbox_rejection(self):
        geometry = {
            "type": "Polygon",
            "coordinates": [[[13.0, 52.0], [13.1, 52.0],
                             [13.1, 52.1], [13.0, 52.0]]],
        }
        env = _geometry_envelope(geometry)
        self.assertEqual(env, (13.0, 52.0, 13.1, 52.1))
        self.assertFalse(_bbox_overlaps(env, (12, 51, 12.5, 51.5)))
        self.assertTrue(_bbox_inside(env, (12, 51, 14, 53)))

    def test_resume_download_http_206(self):
        url = "https://bfppub.blob.core.windows.net/data/test.csv.gz"
        match = {"quadkey": "120212010", "url": url, "location": "Germany"}
        payload = gzip.compress(b'gap tests\n' * 10)
        with tempfile.TemporaryDirectory() as tmp:
            partial = _cache_path(tmp, match).with_suffix(".gz.part")
            partial.parent.mkdir(parents=True, exist_ok=True)
            partial.write_bytes(payload[:12])
            response = FakeResponse(
                payload[12:],
                {"Content-Length": str(len(payload) - 12),
                 "Content-Range": f"bytes 12-{len(payload) - 1}/{len(payload)}"},
                status_code=206,
            )
            with patch("microsoft_buildings.requests.get", return_value=response) as get:
                path, cached = download_tile(match, tmp)
            self.assertFalse(cached)
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(get.call_args.kwargs["headers"]["Range"], "bytes=12-")

    def test_ignored_range_restarts_cleanly(self):
        url = "https://bfppub.blob.core.windows.net/data/test.csv.gz"
        match = {"quadkey": "120212010", "url": url, "location": "Germany"}
        payload = gzip.compress(b'building\n' * 50)
        with tempfile.TemporaryDirectory() as tmp:
            partial = _cache_path(tmp, match).with_suffix(".gz.part")
            partial.parent.mkdir(parents=True, exist_ok=True)
            partial.write_bytes(b"old incomplete bytes")
            with patch("microsoft_buildings.requests.get", return_value=FakeResponse(payload)):
                path, _ = download_tile(match, tmp)
            self.assertEqual(path.read_bytes(), payload)

    def test_job_key_changes_with_extent(self):
        matches = [{"quadkey": "120212010", "url": "https://bfppub.blob.core.windows.net/a"}]
        a = _job_key(matches, (13.03, 52.38, 13.10, 52.43))
        b = _job_key(matches, (13.03, 52.38, 13.11, 52.43))
        self.assertNotEqual(a, b)

    def test_height_and_confidence_null(self):
        self.assertIsNone(_optional_float(-1))
        self.assertIsNone(_optional_float(None))
        self.assertEqual(_optional_float(3.4), 3.4)


if __name__ == "__main__":
    unittest.main()
