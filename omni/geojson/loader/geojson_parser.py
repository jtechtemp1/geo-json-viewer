"""GeoJSON parser used by the GeoJSON Loader extension.

The parser is intentionally dependency free: it relies on the Python standard
library only, so it works inside Kit's embedded interpreter without extra wheels.

It implements a strict subset of RFC 7946:

    * FeatureCollection / Feature
    * Geometry types: Point, MultiPoint, LineString, MultiLineString,
      Polygon, MultiPolygon, GeometryCollection
    * Coordinates are expected in [longitude, latitude] order, optional Z.

The parser does **not** reproject coordinates. The USD builder is responsible
for converting longitude/latitude into local meters (or leaving them as-is if
the user disables projection).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------
Coord = Tuple[float, float, float]           # always (x, y, z) - z may be 0.0
Ring = List[Coord]                           # closed ring for a Polygon
PolygonRings = List[Ring]                    # outer + holes


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class GeoJsonError(Exception):
    """Raised when the supplied document is not valid GeoJSON."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class Geometry:
    """Normalized geometry used inside the extension.

    The original GeoJSON ``type`` is preserved so the USD builder can decide
    how to materialize it (Points / BasisCurves / Mesh).
    """

    type: str
    # Coordinates are normalized into a single, type-specific shape:
    #   Point            -> Coord
    #   MultiPoint       -> List[Coord]
    #   LineString       -> List[Coord]
    #   MultiLineString  -> List[List[Coord]]
    #   Polygon          -> PolygonRings
    #   MultiPolygon     -> List[PolygonRings]
    coordinates: Any = None
    # GeometryCollection carries child geometries instead of coordinates.
    geometries: List["Geometry"] = field(default_factory=list)


@dataclass
class Feature:
    """A normalized GeoJSON feature."""

    geometry: Optional[Geometry]
    properties: Dict[str, Any] = field(default_factory=dict)
    id: Optional[Union[str, int]] = None


@dataclass
class FeatureCollection:
    """Top level container returned by :func:`parse_geojson`."""

    features: List[Feature] = field(default_factory=list)
    # Bounding box in input CRS: (min_lon, min_lat, max_lon, max_lat).
    bbox: Optional[Tuple[float, float, float, float]] = None
    source_path: Optional[str] = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def parse_geojson_file(path: Union[str, Path]) -> FeatureCollection:
    """Parse a GeoJSON file from disk.

    :param path: Absolute or relative path to a .geojson / .json file.
    :raises GeoJsonError: when the file cannot be read or is not valid.
    """
    p = Path(path)
    if not p.is_file():
        raise GeoJsonError(f"GeoJSON file not found: {p}")
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise GeoJsonError(f"Cannot read {p}: {exc}") from exc

    fc = parse_geojson(text)
    fc.source_path = str(p)
    return fc


def parse_geojson(text_or_dict: Union[str, Dict[str, Any]]) -> FeatureCollection:
    """Parse a GeoJSON document from text or a pre-decoded dict."""
    if isinstance(text_or_dict, str):
        try:
            doc = json.loads(text_or_dict)
        except json.JSONDecodeError as exc:
            raise GeoJsonError(f"Invalid JSON: {exc}") from exc
    elif isinstance(text_or_dict, dict):
        doc = text_or_dict
    else:
        raise GeoJsonError("parse_geojson expects str or dict input")

    return _parse_root(doc)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
_GEOMETRY_TYPES = {
    "Point",
    "MultiPoint",
    "LineString",
    "MultiLineString",
    "Polygon",
    "MultiPolygon",
    "GeometryCollection",
}


def _parse_root(doc: Dict[str, Any]) -> FeatureCollection:
    t = doc.get("type")
    if t == "FeatureCollection":
        features = [_parse_feature(f) for f in doc.get("features", [])]
    elif t == "Feature":
        features = [_parse_feature(doc)]
    elif t in _GEOMETRY_TYPES:
        # Wrap bare geometries in a synthetic feature so downstream code stays uniform.
        features = [Feature(geometry=_parse_geometry(doc), properties={}, id=None)]
    else:
        raise GeoJsonError(f"Unsupported root 'type': {t!r}")

    bbox = _read_bbox(doc) or _compute_bbox(features)
    return FeatureCollection(features=features, bbox=bbox)


def _parse_feature(obj: Dict[str, Any]) -> Feature:
    if obj.get("type") != "Feature":
        raise GeoJsonError(f"Expected Feature, got {obj.get('type')!r}")
    geom_obj = obj.get("geometry")
    geom = _parse_geometry(geom_obj) if geom_obj else None
    props = obj.get("properties") or {}
    if not isinstance(props, dict):
        raise GeoJsonError("Feature.properties must be an object or null")
    return Feature(geometry=geom, properties=props, id=obj.get("id"))


def _parse_geometry(obj: Dict[str, Any]) -> Geometry:
    t = obj.get("type")
    if t not in _GEOMETRY_TYPES:
        raise GeoJsonError(f"Unsupported geometry type: {t!r}")

    if t == "GeometryCollection":
        children = [_parse_geometry(g) for g in obj.get("geometries", [])]
        return Geometry(type=t, geometries=children)

    raw = obj.get("coordinates")
    if raw is None:
        raise GeoJsonError(f"{t} is missing 'coordinates'")

    if t == "Point":
        coords: Any = _coord(raw)
    elif t == "MultiPoint":
        coords = [_coord(c) for c in raw]
    elif t == "LineString":
        coords = [_coord(c) for c in raw]
        if len(coords) < 2:
            raise GeoJsonError("LineString needs at least 2 positions")
    elif t == "MultiLineString":
        coords = [[_coord(c) for c in line] for line in raw]
    elif t == "Polygon":
        coords = _polygon(raw)
    elif t == "MultiPolygon":
        coords = [_polygon(p) for p in raw]
    else:  # pragma: no cover - guarded by the membership check above
        raise GeoJsonError(f"Unhandled geometry type: {t}")

    return Geometry(type=t, coordinates=coords)


def _polygon(rings: Iterable[Iterable[Iterable[float]]]) -> PolygonRings:
    out: PolygonRings = []
    for i, ring in enumerate(rings):
        pts = [_coord(c) for c in ring]
        if len(pts) < 4:
            raise GeoJsonError("Polygon ring needs at least 4 positions")
        if pts[0] != pts[-1]:
            raise GeoJsonError("Polygon ring must be closed (first == last)")
        out.append(pts)
        del i  # explicit unused
    return out


def _coord(c: Iterable[float]) -> Coord:
    items = list(c)
    if len(items) < 2:
        raise GeoJsonError(f"Position needs >=2 components, got {items!r}")
    x = float(items[0])
    y = float(items[1])
    z = float(items[2]) if len(items) >= 3 else 0.0
    if not all(math.isfinite(v) for v in (x, y, z)):
        raise GeoJsonError(f"Non finite position: {items!r}")
    return (x, y, z)


def _read_bbox(doc: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:
    bbox = doc.get("bbox")
    if not bbox:
        return None
    if len(bbox) == 4:
        return tuple(float(v) for v in bbox)  # type: ignore[return-value]
    if len(bbox) == 6:
        # 3D bbox: drop Z dimensions for the planar bbox used by the UI.
        return (float(bbox[0]), float(bbox[1]), float(bbox[3]), float(bbox[4]))
    raise GeoJsonError(f"Invalid bbox length: {len(bbox)}")


def _compute_bbox(features: List[Feature]) -> Optional[Tuple[float, float, float, float]]:
    min_x = min_y = math.inf
    max_x = max_y = -math.inf
    seen = False
    for f in features:
        for x, y, _ in _iter_positions(f.geometry):
            seen = True
            if x < min_x:
                min_x = x
            if y < min_y:
                min_y = y
            if x > max_x:
                max_x = x
            if y > max_y:
                max_y = y
    if not seen:
        return None
    return (min_x, min_y, max_x, max_y)


def _iter_positions(geom: Optional[Geometry]) -> Iterable[Coord]:
    if geom is None:
        return
    if geom.type == "GeometryCollection":
        for child in geom.geometries:
            yield from _iter_positions(child)
        return
    if geom.type == "Point":
        yield geom.coordinates
    elif geom.type in ("MultiPoint", "LineString"):
        yield from geom.coordinates
    elif geom.type == "MultiLineString":
        for line in geom.coordinates:
            yield from line
    elif geom.type == "Polygon":
        for ring in geom.coordinates:
            yield from ring
    elif geom.type == "MultiPolygon":
        for poly in geom.coordinates:
            for ring in poly:
                yield from ring


# ---------------------------------------------------------------------------
# Convenience helpers consumed by the UI / USD builder
# ---------------------------------------------------------------------------
def summarize(fc: FeatureCollection) -> Dict[str, Any]:
    """Return a small JSON-friendly summary for the UI panel."""
    counts: Dict[str, int] = {}
    for feat in fc.features:
        if feat.geometry is None:
            counts["Empty"] = counts.get("Empty", 0) + 1
            continue
        counts[feat.geometry.type] = counts.get(feat.geometry.type, 0) + 1
    return {
        "feature_count": len(fc.features),
        "by_type": counts,
        "bbox": list(fc.bbox) if fc.bbox else None,
        "source": fc.source_path,
    }
