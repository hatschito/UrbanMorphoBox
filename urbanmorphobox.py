# -*- coding: utf-8 -*-
"""
/***************************************************************************
 UrbanMorphoBox
                                 A QGIS plugin
 Download and analyze OSM and Microsoft building footprints within a bounding box
 ***************************************************************************/
"""

import os.path
import time
from pathlib import Path
from qgis.PyQt.QtWidgets import QInputDialog

import requests

from qgis.PyQt.QtCore import (
    QCoreApplication,
    QSettings,
    QTranslator,
    Qt,
    QVariant,
)
from qgis.PyQt.QtGui import QColor, QIcon
from qgis.PyQt.QtWidgets import (
    QAction,
    QApplication,
    QMessageBox,
    QProgressDialog,
)
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsDistanceArea,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsGraduatedSymbolRenderer,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsRendererRange,
    QgsSymbol,
    QgsTask,
    QgsApplication,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsMapTool, QgsRubberBand

from .resources import *
from .urbanmorphobox_dialog import UrbanMorphoBoxDialog
from .microsoft_buildings import (
    Cancelled as MicrosoftCancelled,
    DATASET_RELEASE as MICROSOFT_RELEASE,
    find_microsoft_tiles,
    cache_status,
    download_and_export,
)


class RectangleMapTool(QgsMapTool):
    """Map tool to draw a rectangle on the QGIS canvas."""

    def __init__(self, canvas, callback):
        super().__init__(canvas)
        self.canvas = canvas
        self.callback = callback
        self.start_point = None
        self.end_point = None
        self.is_drawing = False

        self.rubber_band = QgsRubberBand(
            self.canvas,
            QgsWkbTypes.PolygonGeometry,
        )
        self.rubber_band.setColor(QColor(255, 0, 0, 80))
        self.rubber_band.setWidth(2)

    def canvasPressEvent(self, event):
        """Start drawing the rectangle."""
        self.start_point = self.toMapCoordinates(event.pos())
        self.end_point = self.start_point
        self.is_drawing = True
        self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)

    def canvasMoveEvent(self, event):
        """Update the rectangle while moving the mouse."""
        if not self.is_drawing:
            return

        self.end_point = self.toMapCoordinates(event.pos())
        self.update_rectangle()

    def canvasReleaseEvent(self, event):
        """Finish drawing and pass the rectangle to the callback."""
        if not self.is_drawing:
            return

        self.end_point = self.toMapCoordinates(event.pos())
        self.update_rectangle()

        xmin = min(self.start_point.x(), self.end_point.x())
        xmax = max(self.start_point.x(), self.end_point.x())
        ymin = min(self.start_point.y(), self.end_point.y())
        ymax = max(self.start_point.y(), self.end_point.y())

        rectangle = QgsRectangle(xmin, ymin, xmax, ymax)

        self.is_drawing = False
        self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        self.callback(rectangle)

    def update_rectangle(self):
        """Update the visible rubber-band rectangle."""
        if self.start_point is None or self.end_point is None:
            return

        p1 = self.start_point
        p2 = self.end_point

        points = [
            QgsPointXY(p1.x(), p1.y()),
            QgsPointXY(p2.x(), p1.y()),
            QgsPointXY(p2.x(), p2.y()),
            QgsPointXY(p1.x(), p2.y()),
            QgsPointXY(p1.x(), p1.y()),
        ]

        self.rubber_band.setToGeometry(
            QgsGeometry.fromPolygonXY([points]),
            None,
        )


class MicrosoftLookupTask(QgsTask):
    """Lookup download URLs without touching the QGIS GUI from a worker thread."""

    def __init__(self, bbox, cache_dir, on_finished):
        super().__init__("UrbanMorphoBox: locate Microsoft tiles", QgsTask.CanCancel)
        self.bbox = bbox
        self.cache_dir = cache_dir
        self.on_finished = on_finished
        self.matches = []
        self.error = None

    def run(self):
        try:
            self.matches = find_microsoft_tiles(
                self.bbox, self.cache_dir, cancelled=self.isCanceled
            )
            return not self.isCanceled()
        except MicrosoftCancelled:
            return False
        except Exception as error:
            self.error = str(error)
            return False

    def finished(self, success):
        # QgsTask.finished() executes on the QGIS main (GUI) thread.
        self.on_finished(success, self.matches, self.error, self.bbox)


class MicrosoftDownloadTask(QgsTask):
    """Download, spatially clip, and export in a background QGIS task."""

    def __init__(self, matches, bbox, cache_dir, output_dir, on_finished):
        super().__init__("UrbanMorphoBox: download Microsoft buildings", QgsTask.CanCancel)
        self.matches = matches
        self.bbox = bbox
        self.cache_dir = cache_dir
        self.output_dir = output_dir
        self.on_finished = on_finished
        self.result_data = None
        self.error = None

    def run(self):
        try:
            self.result_data = download_and_export(
                self.matches,
                self.bbox,
                self.cache_dir,
                self.output_dir,
                cancelled=self.isCanceled,
                report=self.setProgress,
            )
            return not self.isCanceled()
        except MicrosoftCancelled:
            return False
        except Exception as error:
            self.error = str(error)
            return False

    def finished(self, success):
        # All QGIS layers and dialogs must be created on the main thread.
        self.on_finished(success, self.result_data, self.error)


class UrbanMorphoBox:
    """QGIS plugin implementation."""

    OVERPASS_URL = "https://overpass-api.de/api/interpreter"
    RETRIABLE_HTTP_CODES = {429, 500, 502, 503, 504}
    RETRY_DELAYS_SECONDS = (2, 5, 10)

    def __init__(self, iface):
        """Initialize the plugin."""
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)

        locale_value = QSettings().value("locale/userLocale", "en")
        locale = str(locale_value)[0:2]
        locale_path = os.path.join(
            self.plugin_dir,
            "i18n",
            f"UrbanMorphoBox_{locale}.qm",
        )

        if os.path.exists(locale_path):
            self.translator = QTranslator()
            self.translator.load(locale_path)
            QCoreApplication.installTranslator(self.translator)

        self.actions = []
        self.menu = self.tr("&UrbanMorphoBox")
        self.first_start = None
        self.rectangle_tool = None
        self.microsoft_lookup_task = None
        self.microsoft_download_task = None

    def tr(self, message):
        """Translate a message."""
        return QCoreApplication.translate("UrbanMorphoBox", message)

    def add_action(
        self,
        icon_path,
        text,
        callback,
        enabled_flag=True,
        add_to_menu=True,
        add_to_toolbar=True,
        status_tip=None,
        whats_this=None,
        parent=None,
    ):
        """Add an action to the QGIS interface."""
        icon = QIcon(icon_path)
        action = QAction(icon, text, parent)
        action.triggered.connect(callback)
        action.setEnabled(enabled_flag)

        if status_tip is not None:
            action.setStatusTip(status_tip)

        if whats_this is not None:
            action.setWhatsThis(whats_this)

        if add_to_toolbar:
            self.iface.addToolBarIcon(action)

        if add_to_menu:
            self.iface.addPluginToMenu(self.menu, action)

        self.actions.append(action)
        return action

    def initGui(self):
        """Create menu entries and toolbar icons."""
        icon_path = ":/plugins/urbanmorphobox/icon.png"

        self.add_action(
            icon_path,
            text=self.tr("UrbanMorphoBox"),
            callback=self.run,
            parent=self.iface.mainWindow(),
        )

        self.first_start = True

    def unload(self):
        """Remove plugin menu entries and toolbar icons."""
        if self.microsoft_lookup_task is not None:
            self.microsoft_lookup_task.cancel()
        if self.microsoft_download_task is not None:
            self.microsoft_download_task.cancel()
        for action in self.actions:
            self.iface.removePluginMenu(
                self.tr("&UrbanMorphoBox"),
                action,
            )
            self.iface.removeToolBarIcon(action)

    def run(self):
        """Select OSM or Microsoft, then let the user draw a study rectangle."""
        source, ok = QInputDialog.getItem(
            self.iface.mainWindow(),
            "UrbanMorphoBox – Data Source",
            "Building data source:",
            ["OpenStreetMap", "Microsoft Global ML Building Footprints"],
            0,
            False,
        )
        if not ok:
            return
        self.iface.messageBar().pushInfo(
            "UrbanMorphoBox", f"{source}: draw a rectangle on the map."
        )
        self.rectangle_tool = RectangleMapTool(
            self.iface.mapCanvas(),
            lambda extent: self.download_from_source(extent, source),
        )
        self.iface.mapCanvas().setMapTool(self.rectangle_tool)

    def download_from_source(self, extent, source):
        """Dispatch rectangle downloads to the selected data provider."""
        if source == "OpenStreetMap":
            self.download_buildings_from_extent(extent)
        elif source == "Microsoft Global ML Building Footprints":
            self.download_microsoft_buildings(extent)

    def download_microsoft_buildings(self, extent):
        """Start a background lookup for the Microsoft level-9 download tiles."""
        if self.microsoft_lookup_task is not None or self.microsoft_download_task is not None:
            QMessageBox.information(
                self.iface.mainWindow(), "UrbanMorphoBox",
                "A Microsoft download or lookup is already running."
            )
            return
        try:
            source_crs = self.iface.mapCanvas().mapSettings().destinationCrs()
            target_crs = QgsCoordinateReferenceSystem("EPSG:4326")
            transform = QgsCoordinateTransform(
                source_crs, target_crs, QgsProject.instance()
            )
            rect = transform.transformBoundingBox(extent)
            bbox = (
                rect.xMinimum(), rect.yMinimum(),
                rect.xMaximum(), rect.yMaximum(),
            )
        except Exception as error:
            QMessageBox.warning(
                self.iface.mainWindow(), "UrbanMorphoBox",
                f"Could not transform study area to WGS84:\n{error}"
            )
            return
        cache_dir = Path.home() / ".cache" / "UrbanMorphoBox" / "microsoft" / MICROSOFT_RELEASE
        task = MicrosoftLookupTask(bbox, cache_dir, self._microsoft_lookup_finished)
        self.microsoft_lookup_task = task
        QgsApplication.taskManager().addTask(task)
        self.show_status("UrbanMorphoBox: looking up Microsoft tile index ...", 10000)

    def _microsoft_lookup_finished(self, success, matches, error, bbox):
        self.microsoft_lookup_task = None
        if not success:
            if error:
                QMessageBox.warning(
                    self.iface.mainWindow(), "UrbanMorphoBox – Microsoft lookup failed", error
                )
            return  # Canceled tasks should not pop up a warning.
        if not matches:
            QMessageBox.information(
                self.iface.mainWindow(), "UrbanMorphoBox",
                "No Microsoft footprint files were found for this rectangle."
            )
            return
        tile_keys = sorted({item["quadkey"] for item in matches})
        tile_names = ", ".join(tile_keys[:8])
        if len(tile_keys) > 8:
            tile_names += f", ... (+{len(tile_keys) - 8} more)"
        cache_dir = Path.home() / ".cache" / "UrbanMorphoBox" / "microsoft" / MICROSOFT_RELEASE
        cached_count, cached_bytes = cache_status(matches, cache_dir)
        message = (
            f"Microsoft dataset: {MICROSOFT_RELEASE}\n"
            f"Files in study area: {len(matches)}\n"
            f"Already cached: {cached_count} ({cached_bytes / 1024**2:,.0f} MiB)\n"
            f"Level-9 quadkeys: {tile_names}\n\n"
            "The full source tiles may be much larger than your rectangle.\n"
            "Downloads run with up to 2 connections, can be canceled,\n"
            "and continue from previous partial downloads when supported.\n"
            "Only buildings within the rectangle enter the GeoPackage.\n\n"
            "Start download? (Progress/cancel: QGIS Task Manager)"
        )
        reply = QMessageBox.question(
            self.iface.mainWindow(), "UrbanMorphoBox – Microsoft buildings",
            message, QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return
        output_dir = Path.home() / ".local" / "share" / "UrbanMorphoBox" / "outputs"
        task = MicrosoftDownloadTask(
            matches, bbox, cache_dir, output_dir, self._microsoft_download_finished
        )
        self.microsoft_download_task = task
        QgsApplication.taskManager().addTask(task)
        self.show_status("UrbanMorphoBox: Microsoft buildings download running ...", 10000)

    def _microsoft_download_finished(self, success, result, error):
        self.microsoft_download_task = None
        if not success:
            if error:
                QMessageBox.warning(
                    self.iface.mainWindow(), "UrbanMorphoBox – Microsoft download failed",
                    error,
                )
            else:
                self.show_status("UrbanMorphoBox: Microsoft download canceled", 10000)
            return
        path = result["path"]
        layer = QgsVectorLayer(
            path + "|layername=microsoft_buildings",
            f"Microsoft Buildings ({result['features']})",
            "ogr",
        )
        if not layer.isValid():
            QMessageBox.warning(
                self.iface.mainWindow(), "UrbanMorphoBox",
                "The downloaded GeoPackage could not be opened in QGIS:\n" + path
            )
            return
        layer.setCustomProperty("UrbanMorphoBox/source", "Microsoft")
        layer.setCustomProperty("UrbanMorphoBox/dataset_release", MICROSOFT_RELEASE)
        layer.setCustomProperty("UrbanMorphoBox/footprint_license", "CDLA Permissive 2.0")
        symbol = QgsSymbol.defaultSymbol(layer.geometryType())
        if symbol:
            symbol.setColor(QColor(80, 140, 220, 180))
            layer.renderer().setSymbol(symbol)
        QgsProject.instance().addMapLayer(layer)
        n = result["features"]
        message = (
            f"Microsoft buildings: {n:,}\n"
            f"Footprint area: {result['area_m2']:,.0f} m²\n"
            f"Downloaded files: {result['download_files']} "
            f"({result['cached_files']} from cache)\n"
            f"Invalid features skipped: {result['invalid']:,}\n"
            f"Resumed tiles/checkpoints: {result.get('resumed_tiles', 0)}\n"
            f"Reused finished output: {'yes' if result.get('reused_output') else 'no'}\n\n"
            f"GeoPackage:\n{path}"
        )
        QMessageBox.information(self.iface.mainWindow(), "UrbanMorphoBox", message)
        self.show_status(f"UrbanMorphoBox: {n:,} Microsoft buildings loaded", 15000)

    def show_status(self, message, timeout_ms=0):
        """Show a message in the QGIS status bar."""
        self.iface.mainWindow().statusBar().showMessage(
            message,
            timeout_ms,
        )

    def wait_with_events(self, seconds, progress=None):
        """Wait while keeping the GUI responsive enough for cancellation."""
        end_time = time.monotonic() + seconds

        while time.monotonic() < end_time:
            QApplication.processEvents()

            if progress is not None and progress.wasCanceled():
                return False

            time.sleep(0.1)

        return True

    def create_tiles_from_extent(self, extent_wgs84, tile_size_deg=0.01):
        """Create rectangular tiles from a WGS84 bounding box."""
        if tile_size_deg <= 0:
            raise ValueError("tile_size_deg must be greater than zero")

        tiles = []

        west = extent_wgs84.xMinimum()
        east = extent_wgs84.xMaximum()
        south = extent_wgs84.yMinimum()
        north = extent_wgs84.yMaximum()

        x = west

        while x < east:
            next_x = min(x + tile_size_deg, east)
            y = south

            while y < north:
                next_y = min(y + tile_size_deg, north)
                tiles.append(QgsRectangle(x, y, next_x, next_y))
                y = next_y

            x = next_x

        return tiles

    def add_tile_debug_layer(self, tiles):
        """Add a temporary polygon layer showing generated tiles."""
        layer = QgsVectorLayer(
            "Polygon?crs=EPSG:4326",
            "UrbanMorphoBox Tiles",
            "memory",
        )

        provider = layer.dataProvider()
        provider.addAttributes([
            QgsField("tile_id", QVariant.Int),
        ])
        layer.updateFields()

        features = []

        for tile_id, tile in enumerate(tiles, start=1):
            feature = QgsFeature()
            feature.setGeometry(QgsGeometry.fromRect(tile))
            feature.setAttributes([tile_id])
            features.append(feature)

        provider.addFeatures(features)
        layer.updateExtents()

        symbol = QgsSymbol.defaultSymbol(layer.geometryType())
        symbol.setColor(QColor(120, 120, 120, 70))

        symbol_layer = symbol.symbolLayer(0)
        if symbol_layer is not None:
            symbol_layer.setStrokeColor(QColor(60, 60, 60, 180))
            symbol_layer.setStrokeWidth(0.4)

        layer.renderer().setSymbol(symbol)
        layer.triggerRepaint()
        QgsProject.instance().addMapLayer(layer)

    def fetch_buildings_for_tile(
        self,
        tile,
        tile_number,
        total_tiles,
        progress=None,
        max_retries=3,
    ):
        """Download OSM building elements for one WGS84 tile with retries."""
        south = tile.yMinimum()
        west = tile.xMinimum()
        north = tile.yMaximum()
        east = tile.xMaximum()
        bbox = f"{south},{west},{north},{east}"

        query = f"""
        [out:json][timeout:25];
        (
          way["building"]({bbox});
        );
        out geom;
        """

        headers = {
            "User-Agent": "UrbanMorphoBox QGIS Plugin/0.2",
        }

        last_error = None

        for attempt in range(1, max_retries + 1):
            if progress is not None and progress.wasCanceled():
                raise RuntimeError("Canceled by user")

            self.show_status(
                "UrbanMorphoBox: downloading "
                f"tile {tile_number}/{total_tiles}, "
                f"attempt {attempt}/{max_retries}",
            )

            if progress is not None:
                progress.setLabelText(
                    f"Downloading tile {tile_number} of {total_tiles}\n"
                    f"Attempt {attempt} of {max_retries}"
                )
                QApplication.processEvents()

            try:
                response = requests.post(
                    self.OVERPASS_URL,
                    data={"data": query},
                    headers=headers,
                    timeout=(10, 40),
                )

                if response.status_code == 200:
                    data = response.json()
                    return data.get("elements", [])

                response_hint = " ".join(response.text.split())[:160]
                last_error = RuntimeError(
                    f"HTTP {response.status_code}"
                    + (f": {response_hint}" if response_hint else "")
                )

                if response.status_code not in self.RETRIABLE_HTTP_CODES:
                    raise last_error

            except requests.RequestException as error:
                last_error = RuntimeError(
                    f"Network error: {error}"
                )
            except ValueError as error:
                raise RuntimeError(
                    f"Invalid JSON response: {error}"
                ) from error

            if attempt < max_retries:
                delay = self.RETRY_DELAYS_SECONDS[
                    min(attempt - 1, len(self.RETRY_DELAYS_SECONDS) - 1)
                ]

                self.show_status(
                    "UrbanMorphoBox: tile "
                    f"{tile_number}/{total_tiles} failed; "
                    f"retrying in {delay} seconds",
                )

                if progress is not None:
                    progress.setLabelText(
                        f"Tile {tile_number} of {total_tiles} failed.\n"
                        f"Retrying in {delay} seconds..."
                    )

                if not self.wait_with_events(delay, progress):
                    raise RuntimeError("Canceled by user")

        raise RuntimeError(
            f"Failed after {max_retries} attempts: {last_error}"
        )

    def download_buildings_from_extent(self, extent):
        """Download buildings from OSM using tiled Overpass requests."""
        max_features = 10000
        max_bbox_area_deg = 0.05
        max_tiles = 200
        tile_size_deg = 0.01
        request_pause_seconds = 0.4

        progress = None

        try:
            canvas = self.iface.mapCanvas()
            source_crs = canvas.mapSettings().destinationCrs()
            target_crs = QgsCoordinateReferenceSystem("EPSG:4326")

            transform = QgsCoordinateTransform(
                source_crs,
                target_crs,
                QgsProject.instance(),
            )
            extent_wgs84 = transform.transformBoundingBox(extent)

            tiles = self.create_tiles_from_extent(
                extent_wgs84,
                tile_size_deg=tile_size_deg,
            )

            if not tiles:
                QMessageBox.warning(
                    None,
                    "UrbanMorphoBox",
                    "No download tiles could be generated.",
                )
                return

            if len(tiles) > max_tiles:
                QMessageBox.warning(
                    None,
                    "UrbanMorphoBox",
                    f"The selected area would create {len(tiles)} tiles.\n\n"
                    f"The current limit is {max_tiles} tiles.\n\n"
                    "Please select a smaller area.",
                )
                return

            self.add_tile_debug_layer(tiles)

            distance_area = QgsDistanceArea()
            distance_area.setSourceCrs(
                QgsCoordinateReferenceSystem("EPSG:4326"),
                QgsProject.instance().transformContext(),
            )
            distance_area.setEllipsoid("WGS84")

            rectangle_geometry = QgsGeometry.fromRect(extent_wgs84)
            rectangle_area_m2 = distance_area.measureArea(rectangle_geometry)
            rectangle_area_km2 = rectangle_area_m2 / 1_000_000

            west = extent_wgs84.xMinimum()
            east = extent_wgs84.xMaximum()
            south = extent_wgs84.yMinimum()
            north = extent_wgs84.yMaximum()
            bbox_area = abs((east - west) * (north - south))

            if bbox_area > max_bbox_area_deg:
                reply = QMessageBox.question(
                    None,
                    "UrbanMorphoBox",
                    "The selected rectangle is quite large.\n\n"
                    f"Approx. BBox area: {bbox_area:.4f} degree²\n"
                    f"Recommended maximum: {max_bbox_area_deg} degree²\n"
                    f"Generated tiles: {len(tiles)}\n\n"
                    "Continue anyway?",
                    QMessageBox.Yes | QMessageBox.No,
                )

                if reply == QMessageBox.No:
                    return

            progress = QProgressDialog(
                "Preparing tiled OSM download...",
                "Cancel",
                0,
                len(tiles),
                self.iface.mainWindow(),
            )
            progress.setWindowTitle("UrbanMorphoBox")
            progress.setWindowModality(Qt.WindowModal)
            progress.setMinimumDuration(0)
            progress.setAutoClose(False)
            progress.setAutoReset(False)
            progress.setValue(0)
            progress.show()
            QApplication.processEvents()

            unique_elements = {}
            failed_tiles = []
            successful_tiles = []
            canceled = False

            for tile_number, tile in enumerate(tiles, start=1):
                if progress.wasCanceled():
                    canceled = True
                    break

                progress.setValue(tile_number - 1)
                progress.setLabelText(
                    f"Downloading tile {tile_number} of {len(tiles)}..."
                )
                QApplication.processEvents()

                try:
                    elements = self.fetch_buildings_for_tile(
                        tile,
                        tile_number,
                        len(tiles),
                        progress=progress,
                        max_retries=3,
                    )

                    for element in elements:
                        element_type = element.get("type")
                        element_id = element.get("id")

                        if element_type is None or element_id is None:
                            continue

                        unique_elements[(element_type, element_id)] = element

                    successful_tiles.append(tile)

                except Exception as error:
                    if str(error) == "Canceled by user":
                        canceled = True
                        break

                    failed_tiles.append(
                        f"Tile {tile_number}: {error}"
                    )

                progress.setValue(tile_number)
                QApplication.processEvents()

                if tile_number < len(tiles):
                    if not self.wait_with_events(
                        request_pause_seconds,
                        progress,
                    ):
                        canceled = True
                        break

            progress.setValue(len(successful_tiles) + len(failed_tiles))
            progress.close()
            progress = None

            elements = list(unique_elements.values())

            if not elements:
                message = "No buildings could be downloaded."

                if canceled:
                    message += "\n\nThe download was canceled by the user."

                if failed_tiles:
                    message += (
                        f"\n\nFailed tiles: {len(failed_tiles)}\n"
                        + "\n".join(failed_tiles[:8])
                    )

                QMessageBox.warning(
                    None,
                    "UrbanMorphoBox",
                    message,
                )
                self.show_status(
                    "UrbanMorphoBox: no buildings downloaded",
                    10000,
                )
                return

            layer = QgsVectorLayer(
                "Polygon?crs=EPSG:4326",
                "OSM Buildings",
                "memory",
            )
            provider = layer.dataProvider()
            provider.addAttributes([
                QgsField("osm_id", QVariant.String),
                QgsField("area_m2", QVariant.Double),
            ])
            layer.updateFields()

            features = []
            total_area = 0.0

            for element in elements:
                if len(features) >= max_features:
                    break

                geometry_data = element.get("geometry")
                if not geometry_data:
                    continue

                points = []

                for node in geometry_data:
                    longitude = node.get("lon")
                    latitude = node.get("lat")

                    if longitude is None or latitude is None:
                        continue

                    points.append(QgsPointXY(longitude, latitude))

                if len(points) < 3:
                    continue

                if points[0] != points[-1]:
                    points.append(points[0])

                geometry = QgsGeometry.fromPolygonXY([points])
                if geometry.isEmpty():
                    continue

                area = distance_area.measureArea(geometry)
                total_area += area

                feature = QgsFeature()
                feature.setGeometry(geometry)
                feature.setAttributes([
                    str(element.get("id")),
                    area,
                ])
                features.append(feature)

            if not features:
                QMessageBox.information(
                    None,
                    "UrbanMorphoBox",
                    "No valid building geometries were found.",
                )
                return

            downloaded_area_m2 = sum(
                distance_area.measureArea(QgsGeometry.fromRect(tile))
                for tile in successful_tiles
            )
            downloaded_area_km2 = downloaded_area_m2 / 1_000_000

            mean_area = total_area / len(features)
            building_density = (
                len(features) / downloaded_area_km2
                if downloaded_area_km2 > 0
                else 0.0
            )

            provider.addFeatures(features)
            layer.updateExtents()
            layer.setName(f"OSM Buildings ({len(features)})")

            symbol_small = QgsSymbol.defaultSymbol(layer.geometryType())
            symbol_small.setColor(QColor(120, 200, 120))

            symbol_medium = QgsSymbol.defaultSymbol(layer.geometryType())
            symbol_medium.setColor(QColor(240, 200, 80))

            symbol_large = QgsSymbol.defaultSymbol(layer.geometryType())
            symbol_large.setColor(QColor(220, 90, 90))

            ranges = [
                QgsRendererRange(0, 100, symbol_small, "< 100 m²"),
                QgsRendererRange(100, 500, symbol_medium, "100 - 500 m²"),
                QgsRendererRange(500, 1_000_000, symbol_large, "> 500 m²"),
            ]

            renderer = QgsGraduatedSymbolRenderer("area_m2", ranges)
            layer.setRenderer(renderer)
            layer.triggerRepaint()
            QgsProject.instance().addMapLayer(layer)

            incomplete = bool(failed_tiles or canceled)
            successful_count = len(successful_tiles)

            if incomplete:
                heading = "Download completed with missing tiles."
            else:
                heading = "Download finished."

            message = (
                f"{heading}\n\n"
                f"Downloaded buildings: {len(features)}\n"
                f"Successful tiles: {successful_count}\n"
                f"Failed tiles: {len(failed_tiles)}\n"
                f"Average area: {mean_area:.1f} m²\n"
                f"Selected rectangle area: {rectangle_area_km2:.2f} km²\n"
                f"Successfully downloaded area: {downloaded_area_km2:.2f} km²\n"
                f"Building density: {building_density:.1f} buildings/km²\n"
                f"Object limit: {max_features}\n\n"
                "Source: drawn rectangle"
            )

            if incomplete:
                message += (
                    "\n\nStatistics are based only on successfully "
                    "downloaded tiles and are therefore incomplete."
                )

            if canceled:
                message += "\n\nThe download was canceled by the user."

            if failed_tiles:
                message += (
                    "\n\nFailed tile details:\n"
                    + "\n".join(failed_tiles[:8])
                )

                if len(failed_tiles) > 8:
                    message += (
                        f"\n... and {len(failed_tiles) - 8} more."
                    )

            self.show_status(
                "UrbanMorphoBox: finished — "
                f"{successful_count} successful, "
                f"{len(failed_tiles)} failed",
                15000,
            )

            if incomplete:
                QMessageBox.warning(
                    None,
                    "UrbanMorphoBox",
                    message,
                )
            else:
                QMessageBox.information(
                    None,
                    "UrbanMorphoBox",
                    message,
                )

        except Exception as error:
            self.show_status(
                "UrbanMorphoBox: download failed",
                10000,
            )
            QMessageBox.critical(
                None,
                "UrbanMorphoBox",
                "An unexpected error occurred:\n\n"
                f"{error}",
            )

        finally:
            if progress is not None:
                progress.close()