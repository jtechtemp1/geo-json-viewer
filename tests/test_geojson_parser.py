"""Unit tests for the GeoJSON parser and projection helpers.

These tests run with plain ``pytest`` - they don't require Kit or USD because
all USD-touching code lives in ``usd_builder.build_stage`` which the tests do
not exercise directly.
"""

from __future__ import annotations

import json
import math
import os
import sys
import unittest
from pathlib import Path

# Make the extension package importable when the tests are run from the repo root.
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from omni.geojson.loader.geojson_parser import (  # noqa: E402
    GeoJsonError,
    parse_geojson,
    parse_geojson_file,
    summarize,
)
from omni.geojson.loader.usd_builder import (  # noqa: E402
    BuildOptions,
    iter_projected,
)


SAMPLE_PATH = ROOT / "data" / "sample.geojson"


class ParserTests(unittest.TestCase):
    def test_parse_sample_file(self) -> None:
        fc = parse_geojson_file(SAMPLE_PATH)
        self.assertEqual(len(fc.features), 3)
        self.assertIsNotNone(fc.bbox)
        types = sorted(f.geometry.type for f in fc.features)
        self.assertEqual(types, ["LineString", "Point", "Polygon"])

    def test_bbox_is_computed_when_missing(self) -> None:
        doc = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [10.0, 20.0]},
                    "properties": {},
                },
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [-5.0, 15.0]},
                    "properties": {},
                },
            ],
        }
        fc = parse_geojson(doc)
        self.assertEqual(fc.bbox, (-5.0, 15.0, 10.0, 20.0))

    def test_polygon_must_be_closed(self) -> None:
        doc = {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1]]],
            },
            "properties": {},
        }
        with self.assertRaises(GeoJsonError):
            parse_geojson(doc)

    def test_bare_geometry_is_wrapped(self) -> None:
        doc = {"type": "Point", "coordinates": [0, 0]}
        fc = parse_geojson(doc)
        self.assertEqual(len(fc.features), 1)
        self.assertEqual(fc.features[0].geometry.type, "Point")

    def test_summary_counts(self) -> None:
        fc = parse_geojson_file(SAMPLE_PATH)
        summary = summarize(fc)
        self.assertEqual(summary["feature_count"], 3)
        self.assertEqual(summary["by_type"]["Point"], 1)
        self.assertEqual(summary["by_type"]["LineString"], 1)
        self.assertEqual(summary["by_type"]["Polygon"], 1)


class ProjectionTests(unittest.TestCase):
    def test_projection_centered_on_bbox(self) -> None:
        fc = parse_geojson_file(SAMPLE_PATH)
        opts = BuildOptions(project_to_meters=True, scale=1.0)
        projected = list(iter_projected(fc, opts))
        # Origin is the bbox center, so positions should be balanced around 0.
        xs = [p[0] for p in projected]
        zs = [p[2] for p in projected]
        self.assertTrue(min(xs) < 0 < max(xs), f"xs={xs}")
        self.assertTrue(min(zs) < 0 < max(zs), f"zs={zs}")
        for x, y, z in projected:
            for value in (x, y, z):
                self.assertTrue(math.isfinite(value))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
