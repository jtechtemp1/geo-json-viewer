# Package entry point.
# Re-export the extension class so Kit can discover it via the manifest.
# The import is guarded so the package can be imported in a plain Python
# environment (e.g. unit tests) where ``omni.ext`` is not installed.
try:
    from .extension import GeoJsonLoaderExtension  # noqa: F401
except ImportError:  # pragma: no cover - happens outside of Kit
    GeoJsonLoaderExtension = None  # type: ignore[assignment]
