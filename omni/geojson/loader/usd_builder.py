"""Convert a parsed :class:`FeatureCollection` into USD prims on the current Stage.

Design notes
------------
* The builder writes everything below a single root Xform (``/World/GeoJSON`` by
  default) so users can isolate or remove the import in one click.
* Each feature lands under a child Xform whose name is derived from the feature
  ``id`` or its position in the input, plus the geometry type.
* Mapping from GeoJSON to USD:

      Point / MultiPoint           ->  UsdGeomPoints
      LineString / MultiLineString ->  UsdGeomBasisCurves (linear)
      Polygon / MultiPolygon       ->  UsdGeomMesh (fan triangulated, holes ignored)
      GeometryCollection           ->  child Xform with the rules above applied

* Coordinates can be reprojected from longitude/latitude (degrees) to local
  meters using a simple equirectangular projection centered on the bbox. This
  is sufficient for an MVP and avoids pulling pyproj into the runtime.
* Feature ``properties`` are written as USD custom attributes (``userProperties:*``)
  on the leaf prim so they remain inspectable from the Property panel.

The module is import-safe outside of Kit: USD libraries are imported lazily and
``build_stage`` reports a friendly error if ``pxr`` is not available.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .geojson_parser import (
    Coord,
    Feature,
    FeatureCollection,
    Geometry,
    PolygonRings,
)

# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------
@dataclass
class BuildOptions:
    """Tunable parameters consumed by :func:`build_stage`."""

    root_path: str = "/World/GeoJSON"
    project_to_meters: bool = True
    scale: float = 1.0
    polygon_height: float = 0.0          # fallback extrusion height (m) when no property matches
    point_radius: float = 0.5             # widths attribute for UsdGeomPoints
    line_width: float = 0.25              # widths attribute for BasisCurves
    layer_name: Optional[str] = None      # used to name the import xform
    # Property keys that drive per-feature extrusion height. The first key that
    # resolves to a number on the feature's ``properties`` wins. Keep this list
    # ordered from most specific to most generic.
    height_property_keys: List[str] = field(
        default_factory=lambda: [
            "height",
            "height_m",
            "building:height",
            "BLDG_HEIGHT",
            "extrude",
            "extrude_m",
        ]
    )


@dataclass
class BuildResult:
    """Summary returned to the UI after a successful import."""

    root_prim_path: str
    feature_prims: List[str]
    skipped: int = 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def build_stage(fc: FeatureCollection, options: BuildOptions) -> BuildResult:
    """Materialize *fc* on the **current** USD stage.

    Must be called from the main thread because USD authoring is not threadsafe.
    """
    try:
        import omni.usd  # type: ignore
        from pxr import Gf, Sdf, Usd, UsdGeom, Vt  # type: ignore  # noqa: F401
    except ImportError as exc:  # pragma: no cover - runtime guard
        raise RuntimeError(
            "USD libraries are not available. Run this inside a Kit application."
        ) from exc

    stage = omni.usd.get_context().get_stage()
    if stage is None:
        raise RuntimeError("No active USD stage. Open or create a stage first.")

    origin = _projection_origin(fc) if options.project_to_meters else (0.0, 0.0)

    root_prim = _ensure_xform(stage, UsdGeom, options.root_path)
    layer_name = _sanitize(options.layer_name or "Layer")
    layer_path = _unique_child_path(stage, options.root_path, layer_name)
    layer_prim = _ensure_xform(stage, UsdGeom, layer_path)
    _set_user_attr(layer_prim, "geojson:source", fc.source_path or "")
    _set_user_attr(layer_prim, "geojson:featureCount", len(fc.features))

    feature_prims: List[str] = []
    skipped = 0
    for index, feature in enumerate(fc.features):
        if feature.geometry is None:
            skipped += 1
            continue
        prim_path = _feature_prim_path(layer_path, feature, index)
        feat_prim = _ensure_xform(stage, UsdGeom, prim_path)
        _write_properties(feat_prim, feature.properties)

        try:
            _author_geometry(
                stage,
                UsdGeom,
                Vt,
                Gf,
                feat_prim.GetPath(),
                feature.geometry,
                origin,
                options,
                feature.properties,
            )
        except Exception as exc:  # noqa: BLE001 - we want to keep importing
            _set_user_attr(feat_prim, "geojson:error", str(exc))
            skipped += 1
            continue

        feature_prims.append(prim_path)

    # del unused symbol to keep linters happy in environments without pxr
    del Sdf, Usd
    return BuildResult(
        root_prim_path=root_prim.GetPath().pathString,
        feature_prims=feature_prims,
        skipped=skipped,
    )


# ---------------------------------------------------------------------------
# Helpers - prim layout
# ---------------------------------------------------------------------------
_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_]")


def _sanitize(text: str) -> str:
    cleaned = _SANITIZE_RE.sub("_", str(text)) or "Item"
    # USD prim names cannot start with a digit.
    if cleaned[0].isdigit():
        cleaned = f"_{cleaned}"
    return cleaned


def _ensure_xform(stage, UsdGeom, path: str):  # noqa: ANN001 - pxr types
    prim = stage.GetPrimAtPath(path)
    if not prim or not prim.IsValid():
        UsdGeom.Xform.Define(stage, path)
        prim = stage.GetPrimAtPath(path)
    return prim


def _unique_child_path(stage, parent: str, name: str) -> str:
    base = f"{parent.rstrip('/')}/{_sanitize(name)}"
    candidate = base
    suffix = 1
    while stage.GetPrimAtPath(candidate) and stage.GetPrimAtPath(candidate).IsValid():
        suffix += 1
        candidate = f"{base}_{suffix:03d}"
    return candidate


def _feature_prim_path(layer_path: str, feature: Feature, index: int) -> str:
    if feature.id is not None:
        name = f"f_{_sanitize(str(feature.id))}"
    else:
        name = f"f_{index:05d}"
    return f"{layer_path}/{name}"


# ---------------------------------------------------------------------------
# Helpers - attributes
# ---------------------------------------------------------------------------
_ATTR_PREFIX = "userProperties:"


def _set_user_attr(prim, key: str, value: Any) -> None:
    attr_name = f"{_ATTR_PREFIX}{_sanitize(key)}"
    # Lazy import to keep the module loadable outside Kit.
    from pxr import Sdf  # type: ignore

    if isinstance(value, bool):
        attr = prim.CreateAttribute(attr_name, Sdf.ValueTypeNames.Bool, custom=True)
    elif isinstance(value, int):
        attr = prim.CreateAttribute(attr_name, Sdf.ValueTypeNames.Int, custom=True)
    elif isinstance(value, float):
        attr = prim.CreateAttribute(attr_name, Sdf.ValueTypeNames.Float, custom=True)
    else:
        attr = prim.CreateAttribute(attr_name, Sdf.ValueTypeNames.String, custom=True)
        value = str(value)
    attr.Set(value)


def _write_properties(prim, props: Dict[str, Any]) -> None:
    for key, value in props.items():
        # Skip nested structures - flatten them to JSON strings.
        if isinstance(value, (dict, list)):
            import json

            _set_user_attr(prim, key, json.dumps(value, ensure_ascii=False))
        else:
            _set_user_attr(prim, key, value)


# ---------------------------------------------------------------------------
# Helpers - projection
# ---------------------------------------------------------------------------
_EARTH_RADIUS_M = 6_378_137.0


def _projection_origin(fc: FeatureCollection) -> Tuple[float, float]:
    if not fc.bbox:
        return (0.0, 0.0)
    min_x, min_y, max_x, max_y = fc.bbox
    return ((min_x + max_x) * 0.5, (min_y + max_y) * 0.5)


def _project(lon: float, lat: float, origin: Tuple[float, float]) -> Tuple[float, float]:
    """Equirectangular projection centered on ``origin`` (degrees)."""
    o_lon, o_lat = origin
    x = math.radians(lon - o_lon) * _EARTH_RADIUS_M * math.cos(math.radians(o_lat))
    y = math.radians(lat - o_lat) * _EARTH_RADIUS_M
    return (x, y)


def _xyz(coord: Coord, origin: Tuple[float, float], options: BuildOptions):
    if options.project_to_meters:
        x, y = _project(coord[0], coord[1], origin)
    else:
        x, y = coord[0], coord[1]
    z = coord[2]
    # USD default up-axis is Y; place plan coordinates on the X/Z plane and
    # use the optional Z value (or extrusion height) on Y.
    return (x * options.scale, z * options.scale, -y * options.scale)


# ---------------------------------------------------------------------------
# Geometry authoring
# ---------------------------------------------------------------------------
def _author_geometry(stage, UsdGeom, Vt, Gf, parent_path, geom: Geometry,
                     origin: Tuple[float, float], options: BuildOptions,
                     properties: Optional[Dict[str, Any]] = None) -> None:
    if geom.type == "GeometryCollection":
        for i, child in enumerate(geom.geometries):
            child_path = f"{parent_path}/g_{i:03d}"
            _author_geometry(stage, UsdGeom, Vt, Gf, child_path, child, origin, options, properties)
        return

    leaf_path = f"{parent_path}/geom"
    if geom.type in ("Point", "MultiPoint"):
        coords = [geom.coordinates] if geom.type == "Point" else geom.coordinates
        _author_points(stage, UsdGeom, Vt, leaf_path, coords, origin, options)
    elif geom.type in ("LineString", "MultiLineString"):
        lines = [geom.coordinates] if geom.type == "LineString" else geom.coordinates
        _author_curves(stage, UsdGeom, Vt, leaf_path, lines, origin, options)
    elif geom.type in ("Polygon", "MultiPolygon"):
        polys = [geom.coordinates] if geom.type == "Polygon" else geom.coordinates
        _author_meshes(stage, UsdGeom, Vt, leaf_path, polys, origin, options, properties)
    else:  # pragma: no cover - parser already filtered unknown types
        raise ValueError(f"Unhandled geometry type: {geom.type}")


def _author_points(stage, UsdGeom, Vt, path, coords: Sequence[Coord],
                   origin, options) -> None:
    pts = UsdGeom.Points.Define(stage, path)
    positions = [_xyz(c, origin, options) for c in coords]
    pts.CreatePointsAttr(Vt.Vec3fArray(positions))
    pts.CreateWidthsAttr(Vt.FloatArray([options.point_radius] * len(positions)))
    pts.SetWidthsInterpolation(UsdGeom.Tokens.vertex)


def _author_curves(stage, UsdGeom, Vt, path, lines: Sequence[Sequence[Coord]],
                   origin, options) -> None:
    curves = UsdGeom.BasisCurves.Define(stage, path)
    all_points: List[Tuple[float, float, float]] = []
    vertex_counts: List[int] = []
    for line in lines:
        verts = [_xyz(c, origin, options) for c in line]
        if len(verts) < 2:
            continue
        all_points.extend(verts)
        vertex_counts.append(len(verts))
    curves.CreatePointsAttr(Vt.Vec3fArray(all_points))
    curves.CreateCurveVertexCountsAttr(Vt.IntArray(vertex_counts))
    curves.CreateTypeAttr(UsdGeom.Tokens.linear)
    curves.CreateWidthsAttr(Vt.FloatArray([options.line_width] * len(all_points)))
    curves.SetWidthsInterpolation(UsdGeom.Tokens.vertex)


def _author_meshes(stage, UsdGeom, Vt, path, polys: Sequence[PolygonRings],
                   origin, options, properties: Optional[Dict[str, Any]] = None) -> None:
    """Author one mesh per feature.

    When *height* (resolved from the feature's properties, falling back to the
    global ``polygon_height`` option) is greater than zero, the polygon is
    extruded into a solid prism with a bottom cap, a top cap and side walls.
    Otherwise the polygon is laid flat as a single triangulated patch.
    """
    height = resolve_extrusion_height(properties, options)

    mesh = UsdGeom.Mesh.Define(stage, path)
    positions: List[Tuple[float, float, float]] = []
    face_vertex_counts: List[int] = []
    face_vertex_indices: List[int] = []

    for poly in polys:
        if not poly:
            continue
        outer = poly[0]
        # Drop the closing duplicate before triangulation.
        ring = outer[:-1] if outer and outer[0] == outer[-1] else outer
        n = len(ring)
        if n < 3:
            continue

        bottom_base = len(positions)
        bottom_xyz = [_xyz(c, origin, options) for c in ring]
        positions.extend(bottom_xyz)

        if height > 0.0:
            top_base = len(positions)
            positions.extend([(x, y + height, z) for (x, y, z) in bottom_xyz])

            # Bottom cap (reverse winding so the normal points down).
            for i in range(1, n - 1):
                face_vertex_counts.append(3)
                face_vertex_indices.extend([
                    bottom_base,
                    bottom_base + i + 1,
                    bottom_base + i,
                ])

            # Top cap (normal points up).
            for i in range(1, n - 1):
                face_vertex_counts.append(3)
                face_vertex_indices.extend([
                    top_base,
                    top_base + i,
                    top_base + i + 1,
                ])

            # Side walls: two triangles per edge, with outward winding that
            # matches a CCW outer ring (standard for GeoJSON).
            for i in range(n):
                j = (i + 1) % n
                b0 = bottom_base + i
                b1 = bottom_base + j
                t0 = top_base + i
                t1 = top_base + j
                face_vertex_counts.append(3)
                face_vertex_indices.extend([b0, b1, t1])
                face_vertex_counts.append(3)
                face_vertex_indices.extend([b0, t1, t0])
        else:
            # Flat patch (no extrusion). Fan triangulation works for convex
            # polygons; concave polygons should turn this into earcut later.
            for i in range(1, n - 1):
                face_vertex_counts.append(3)
                face_vertex_indices.extend([
                    bottom_base,
                    bottom_base + i,
                    bottom_base + i + 1,
                ])

    mesh.CreatePointsAttr(Vt.Vec3fArray(positions))
    mesh.CreateFaceVertexCountsAttr(Vt.IntArray(face_vertex_counts))
    mesh.CreateFaceVertexIndicesAttr(Vt.IntArray(face_vertex_indices))
    mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)


def resolve_extrusion_height(properties: Optional[Dict[str, Any]],
                             options: BuildOptions) -> float:
    """Return the extrusion height (meters * scale) for a feature.

    The first key in ``options.height_property_keys`` that exists on
    ``properties`` and can be coerced to a finite, non-negative float wins.
    Falls back to ``options.polygon_height`` when no key matches.
    """
    if properties:
        for key in options.height_property_keys:
            if key in properties:
                try:
                    value = float(properties[key])
                except (TypeError, ValueError):
                    continue
                if math.isfinite(value) and value >= 0.0:
                    return value * options.scale
    return options.polygon_height * options.scale


# ---------------------------------------------------------------------------
# Public utility used by tests
# ---------------------------------------------------------------------------
def iter_projected(fc: FeatureCollection, options: BuildOptions) -> Iterable[Tuple[float, float, float]]:
    """Yield projected XYZ tuples for every position in *fc*.

    Useful for unit tests where the USD libraries are not available.
    """
    origin = _projection_origin(fc) if options.project_to_meters else (0.0, 0.0)
    from .geojson_parser import _iter_positions  # local import to keep API surface small

    for feature in fc.features:
        for coord in _iter_positions(feature.geometry):
            yield _xyz(coord, origin, options)
