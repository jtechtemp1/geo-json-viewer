"""GeoJSON Loader extension entry point.

This module is loaded by ``omni.kit.app`` when the extension is enabled.
It owns the lifecycle (``on_startup`` / ``on_shutdown``), registers a menu
entry under ``Window`` and lazily instantiates the loader window the first
time the user opens it.
"""

from __future__ import annotations

import json
import os
import traceback
from pathlib import Path
from typing import Optional

import omni.ext
import omni.kit.app
import omni.ui as ui
from omni.kit.menu.utils import add_menu_items, remove_menu_items, MenuItemDescription

from .geojson_parser import (
    FeatureCollection,
    GeoJsonError,
    parse_geojson_file,
    summarize,
)
from .usd_builder import BuildOptions, BuildResult, build_stage

WINDOW_TITLE = "GeoJSON Loader"
MENU_PATH = "Window/GeoJSON Loader"
EXT_SETTINGS_PATH = "/exts/omni.geojson.loader"


class GeoJsonLoaderExtension(omni.ext.IExt):
    """Extension lifecycle hooks called by Kit."""

    # --- lifecycle ---------------------------------------------------------
    def on_startup(self, ext_id: str) -> None:  # noqa: D401
        self._ext_id = ext_id
        self._window: Optional[_LoaderWindow] = None

        self._menu_items = [
            MenuItemDescription(
                name="GeoJSON Loader",
                onclick_fn=self._show_window,
                glyph="none.svg",
            )
        ]
        add_menu_items(self._menu_items, "Window")

    def on_shutdown(self) -> None:  # noqa: D401
        remove_menu_items(self._menu_items, "Window")
        self._menu_items = []
        if self._window is not None:
            self._window.destroy()
            self._window = None

    # --- helpers -----------------------------------------------------------
    def _show_window(self) -> None:
        if self._window is None:
            self._window = _LoaderWindow()
        self._window.show()


# ---------------------------------------------------------------------------
# UI window
# ---------------------------------------------------------------------------
class _LoaderWindow:
    """omni.ui Window that exposes the loader controls."""

    def __init__(self) -> None:
        settings = omni.kit.app.get_app().get_settings()
        self._default_root = settings.get(f"{EXT_SETTINGS_PATH}/default_root_path") or "/World/GeoJSON"
        self._default_scale = float(settings.get(f"{EXT_SETTINGS_PATH}/default_scale") or 1.0)
        self._default_height = float(settings.get(f"{EXT_SETTINGS_PATH}/default_height") or 0.0)
        self._project = bool(settings.get(f"{EXT_SETTINGS_PATH}/project_to_meters") if settings.get(f"{EXT_SETTINGS_PATH}/project_to_meters") is not None else True)

        self._window = ui.Window(WINDOW_TITLE, width=520, height=560)
        self._window.deferred_dock_in("Property")

        # State -------------------------------------------------------------
        self._path_model = ui.SimpleStringModel("")
        self._root_model = ui.SimpleStringModel(self._default_root)
        self._scale_model = ui.SimpleFloatModel(self._default_scale)
        self._height_model = ui.SimpleFloatModel(self._default_height)
        self._project_model = ui.SimpleBoolModel(self._project)
        self._status_model = ui.SimpleStringModel("Idle.")
        self._preview_model = ui.SimpleStringModel("")
        self._fc: Optional[FeatureCollection] = None

        self._build_layout()

    # ------------------------------------------------------------------ UI
    def _build_layout(self) -> None:
        with self._window.frame:
            with ui.VStack(spacing=8, margin=8):
                ui.Label("GeoJSON Loader (MVP)", style={"font_size": 18})
                ui.Separator(height=2)

                # File row ---------------------------------------------------
                with ui.HStack(height=24, spacing=4):
                    ui.Label("File", width=60)
                    ui.StringField(model=self._path_model)
                    ui.Button("Browse...", width=80, clicked_fn=self._open_file_picker)

                # Options group ---------------------------------------------
                with ui.CollapsableFrame("Options", collapsed=False):
                    with ui.VStack(spacing=4, margin=4):
                        self._labeled_field("Root prim", self._root_model)
                        self._labeled_float("Scale", self._scale_model)
                        self._labeled_float("Polygon height (m)", self._height_model)
                        with ui.HStack(height=22, spacing=4):
                            ui.Label("Project lon/lat to meters", width=240)
                            ui.CheckBox(model=self._project_model)

                # Action buttons --------------------------------------------
                with ui.HStack(height=28, spacing=4):
                    ui.Button("Load & Preview", clicked_fn=self._on_load_clicked)
                    ui.Button("Build on Stage", clicked_fn=self._on_build_clicked)
                    ui.Button("Clear Preview", clicked_fn=self._on_clear_clicked)

                # Status -----------------------------------------------------
                with ui.HStack(height=22):
                    ui.Label("Status:", width=60)
                    ui.StringField(model=self._status_model, read_only=True)

                # Preview ----------------------------------------------------
                ui.Label("Preview / Summary")
                with ui.ScrollingFrame(
                    horizontal_scrollbar_policy=ui.ScrollBarPolicy.SCROLLBAR_AS_NEEDED,
                    vertical_scrollbar_policy=ui.ScrollBarPolicy.SCROLLBAR_ALWAYS_ON,
                ):
                    ui.StringField(
                        model=self._preview_model,
                        multiline=True,
                        read_only=True,
                        height=220,
                    )

    def _labeled_field(self, label: str, model: ui.SimpleStringModel) -> None:
        with ui.HStack(height=22, spacing=4):
            ui.Label(label, width=120)
            ui.StringField(model=model)

    def _labeled_float(self, label: str, model: ui.SimpleFloatModel) -> None:
        with ui.HStack(height=22, spacing=4):
            ui.Label(label, width=120)
            ui.FloatDrag(model=model, min=0.0, max=10_000.0, step=0.1)

    # ------------------------------------------------------------- actions
    def show(self) -> None:
        self._window.visible = True

    def destroy(self) -> None:
        if self._window is not None:
            self._window.destroy()
            self._window = None

    def _open_file_picker(self) -> None:
        # Prefer the modern Kit 106 file picker; fall back to a typed-in path.
        try:
            from omni.kit.window.filepicker import FilePickerDialog  # type: ignore
        except ImportError:
            self._status_model.set_value("File picker not available; type a path manually.")
            return

        def _on_apply(filename: str, dirname: str) -> None:
            if not filename:
                return
            full = os.path.join(dirname or "", filename)
            self._path_model.set_value(full)
            picker.hide()

        picker = FilePickerDialog(
            "Select GeoJSON file",
            apply_button_label="Select",
            click_apply_handler=_on_apply,
            file_extension_options=[(".geojson, .json", "GeoJSON files")],
        )
        picker.show()

    def _on_load_clicked(self) -> None:
        path = self._path_model.as_string.strip()
        if not path:
            self._status_model.set_value("Please choose a GeoJSON file first.")
            return
        try:
            self._fc = parse_geojson_file(path)
        except GeoJsonError as exc:
            self._fc = None
            self._status_model.set_value(f"Parse error: {exc}")
            self._preview_model.set_value("")
            return
        except Exception:  # noqa: BLE001 - surface unexpected errors
            self._fc = None
            self._status_model.set_value("Unexpected error - see console.")
            self._preview_model.set_value(traceback.format_exc())
            return

        summary = summarize(self._fc)
        self._status_model.set_value(
            f"Parsed {summary['feature_count']} feature(s) from {Path(path).name}"
        )
        self._preview_model.set_value(json.dumps(summary, indent=2, ensure_ascii=False))

    def _on_build_clicked(self) -> None:
        if self._fc is None:
            self._status_model.set_value("Load a GeoJSON file before building.")
            return
        options = BuildOptions(
            root_path=self._root_model.as_string or "/World/GeoJSON",
            project_to_meters=self._project_model.as_bool,
            scale=float(self._scale_model.as_float) or 1.0,
            polygon_height=float(self._height_model.as_float),
            layer_name=Path(self._fc.source_path or "Layer").stem,
        )
        try:
            result: BuildResult = build_stage(self._fc, options)
        except Exception as exc:  # noqa: BLE001 - keep UI alive
            self._status_model.set_value(f"Build error: {exc}")
            self._preview_model.set_value(traceback.format_exc())
            return
        self._status_model.set_value(
            f"Built {len(result.feature_prims)} prim(s) under {result.root_prim_path}"
            f" (skipped {result.skipped})"
        )

    def _on_clear_clicked(self) -> None:
        self._fc = None
        self._preview_model.set_value("")
        self._status_model.set_value("Cleared.")
