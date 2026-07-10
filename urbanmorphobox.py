# -*- coding: utf-8 -*-
"""
/***************************************************************************
 UrbanMorphoBox
                                 A QGIS plugin
 Download building footprints from OSM within a bounding box
 ***************************************************************************/
"""

import os.path
import requests

from qgis.PyQt.QtCore import QSettings, QTranslator, QCoreApplication, QVariant
from qgis.PyQt.QtGui import QIcon, QColor
from qgis.PyQt.QtWidgets import QAction, QMessageBox

from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsProject,
    QgsVectorLayer,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsField,
    QgsDistanceArea,
    QgsRectangle,
    QgsWkbTypes,
    QgsGraduatedSymbolRenderer,
    QgsRendererRange,
    QgsSymbol
)

from .resources import *
from .urbanmorphobox_dialog import UrbanMorphoBoxDialog


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
            QgsWkbTypes.PolygonGeometry
        )
        self.rubber_band.setColor(QColor(255, 0, 0, 80))
        self.rubber_band.setWidth(2)

    def canvasPressEvent(self, event):
        self.start_point = self.toMapCoordinates(event.pos())
        self.end_point = self.start_point
        self.is_drawing = True
        self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)

    def canvasMoveEvent(self, event):
        if not self.is_drawing:
            return

        self.end_point = self.toMapCoordinates(event.pos())
        self.update_rectangle()

    def canvasReleaseEvent(self, event):
        if not self.is_drawing:
            return

        self.end_point = self.toMapCoordinates(event.pos())
        self.update_rectangle()

        xmin = min(self.start_point.x(), self.end_point.x())
        xmax = max(self.start_point.x(), self.end_point.x())
        ymin = min(self.start_point.y(), self.end_point.y())
        ymax = max(self.start_point.y(), self.end_point.y())

        rect = QgsRectangle(xmin, ymin, xmax, ymax)

        self.is_drawing = False
        self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)

        self.callback(rect)

    def update_rectangle(self):
        if self.start_point is None or self.end_point is None:
            return

        p1 = self.start_point
        p2 = self.end_point

        points = [
            QgsPointXY(p1.x(), p1.y()),
            QgsPointXY(p2.x(), p1.y()),
            QgsPointXY(p2.x(), p2.y()),
            QgsPointXY(p1.x(), p2.y()),
            QgsPointXY(p1.x(), p1.y())
        ]

        self.rubber_band.setToGeometry(
            QgsGeometry.fromPolygonXY([points]),
            None
        )


class UrbanMorphoBox:
    """QGIS Plugin Implementation."""

    def __init__(self, iface):
        """Constructor."""

        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)

        locale = QSettings().value('locale/userLocale')[0:2]
        locale_path = os.path.join(
            self.plugin_dir,
            'i18n',
            'UrbanMorphoBox_{}.qm'.format(locale)
        )

        if os.path.exists(locale_path):
            self.translator = QTranslator()
            self.translator.load(locale_path)
            QCoreApplication.installTranslator(self.translator)

        self.actions = []
        self.menu = self.tr(u'&UrbanMorphoBox')
        self.first_start = None
        self.rectangle_tool = None

    def tr(self, message):
        return QCoreApplication.translate('UrbanMorphoBox', message)

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
        parent=None
    ):

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
            self.iface.addPluginToMenu(
                self.menu,
                action
            )

        self.actions.append(action)

        return action

    def initGui(self):
        """Create menu entries and toolbar icons."""

        icon_path = ':/plugins/urbanmorphobox/icon.png'

        self.add_action(
            icon_path,
            text=self.tr(u'UrbanMorphoBox'),
            callback=self.run,
            parent=self.iface.mainWindow()
        )

        self.first_start = True

    def unload(self):
        """Remove plugin menu item and icon."""

        for action in self.actions:
            self.iface.removePluginMenu(
                self.tr(u'&UrbanMorphoBox'),
                action
            )

            self.iface.removeToolBarIcon(action)

    def run(self):
        """Start rectangle selection tool."""

        QMessageBox.information(
            None,
            "UrbanMorphoBox",
            "Draw a rectangle on the map to download OSM buildings."
        )

        self.rectangle_tool = RectangleMapTool(
            self.iface.mapCanvas(),
            self.download_buildings_from_extent
        )

        self.iface.mapCanvas().setMapTool(self.rectangle_tool)

    def create_tiles_from_extent(self, extent_wgs84, tile_size_deg=0.01):
        """Create rectangular tiles from a WGS84 bounding box."""

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

                tile = QgsRectangle(
                    x,
                    y,
                    next_x,
                    next_y
                )

                tiles.append(tile)

                y = next_y

            x = next_x

        return tiles

    def add_tile_debug_layer(self, tiles):
        """Add a temporary polygon layer showing generated tiles."""

        layer = QgsVectorLayer(
            "Polygon?crs=EPSG:4326",
            "UrbanMorphoBox Tiles",
            "memory"
        )

        provider = layer.dataProvider()

        provider.addAttributes([
            QgsField("tile_id", QVariant.Int)
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
        symbol.symbolLayer(0).setStrokeColor(QColor(60, 60, 60, 180))
        symbol.symbolLayer(0).setStrokeWidth(0.4)

        layer.renderer().setSymbol(symbol)
        layer.triggerRepaint()

        QgsProject.instance().addMapLayer(layer)

    def download_buildings_from_extent(self, extent):
        """Download buildings from OSM using the drawn rectangle extent."""

        max_features = 10000
        max_bbox_area_deg = 0.05

        canvas = self.iface.mapCanvas()

        source_crs = canvas.mapSettings().destinationCrs()
        target_crs = QgsCoordinateReferenceSystem("EPSG:4326")

        transform = QgsCoordinateTransform(
            source_crs,
            target_crs,
            QgsProject.instance()
        )

        extent_wgs84 = transform.transformBoundingBox(extent)

        tiles = self.create_tiles_from_extent(
            extent_wgs84,
            tile_size_deg=0.01
        )

        self.add_tile_debug_layer(tiles)

        rectangle_geom = QgsGeometry.fromRect(extent_wgs84)

        distance_area_rect = QgsDistanceArea()
        distance_area_rect.setSourceCrs(
            QgsCoordinateReferenceSystem("EPSG:4326"),
            QgsProject.instance().transformContext()
        )
        distance_area_rect.setEllipsoid("WGS84")

        rectangle_area_m2 = distance_area_rect.measureArea(rectangle_geom)
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
                f"Recommended maximum: {max_bbox_area_deg} degree²\n\n"
                "Continue anyway?",
                QMessageBox.Yes | QMessageBox.No
            )

            if reply == QMessageBox.No:
                return

        bbox = f"{south},{west},{north},{east}"

        query = f"""
        [out:json][timeout:25];
        (
          way["building"]({bbox});
        );
        out geom;
        """

        url = "https://overpass-api.de/api/interpreter"

        headers = {
            "User-Agent": "UrbanMorphoBox QGIS Plugin/0.1"
        }

        try:
            response = requests.post(
                url,
                data={"data": query},
                headers=headers,
                timeout=30
            )

            if response.status_code == 504:
                QMessageBox.warning(
                    None,
                    "UrbanMorphoBox",
                    "Overpass timeout (504).\n\n"
                    "Try a smaller area or retry later."
                )
                return

            if response.status_code != 200:
                QMessageBox.warning(
                    None,
                    "UrbanMorphoBox",
                    f"Overpass request failed:\n{response.status_code}"
                )
                return

            data = response.json()

            layer_name = "OSM Buildings"

            layer = QgsVectorLayer(
                "Polygon?crs=EPSG:4326",
                layer_name,
                "memory"
            )

            provider = layer.dataProvider()

            provider.addAttributes([
                QgsField("osm_id", QVariant.String),
                QgsField("area_m2", QVariant.Double)
            ])

            layer.updateFields()

            distance_area = QgsDistanceArea()
            distance_area.setSourceCrs(
                QgsCoordinateReferenceSystem("EPSG:4326"),
                QgsProject.instance().transformContext()
            )
            distance_area.setEllipsoid("WGS84")

            features = []
            total_area = 0

            for element in data["elements"]:

                if len(features) >= max_features:
                    break

                if "geometry" not in element:
                    continue

                points = []

                for node in element["geometry"]:
                    points.append(
                        QgsPointXY(
                            node["lon"],
                            node["lat"]
                        )
                    )

                if len(points) < 3:
                    continue

                geom = QgsGeometry.fromPolygonXY([points])
                area = distance_area.measureArea(geom)
                total_area += area

                feature = QgsFeature()
                feature.setGeometry(geom)
                feature.setAttributes([
                    str(element["id"]),
                    area
                ])

                features.append(feature)

            if len(features) == 0:
                QMessageBox.information(
                    None,
                    "UrbanMorphoBox",
                    "No buildings found in the selected area.\n\n"
                    "Try drawing a larger rectangle or selecting another area."
                )
                return

            mean_area = total_area / len(features)

            building_density = 0

            if rectangle_area_km2 > 0:
                building_density = len(features) / rectangle_area_km2

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
                QgsRendererRange(
                    0,
                    100,
                    symbol_small,
                    "< 100 m²"
                ),
                QgsRendererRange(
                    100,
                    500,
                    symbol_medium,
                    "100 - 500 m²"
                ),
                QgsRendererRange(
                    500,
                    1000000,
                    symbol_large,
                    "> 500 m²"
                )
            ]

            renderer = QgsGraduatedSymbolRenderer(
                "area_m2",
                ranges
            )

            layer.setRenderer(renderer)
            layer.triggerRepaint()

            QgsProject.instance().addMapLayer(layer)

            QMessageBox.information(
                None,
                "UrbanMorphoBox",
                "Download finished.\n\n"
                f"Downloaded buildings: {len(features)}\n"
                f"Average area: {mean_area:.1f} m²\n"
                f"Rectangle area: {rectangle_area_km2:.2f} km²\n"
                f"Building density: {building_density:.1f} buildings/km²\n"
                f"Object limit: {max_features}\n\n"
                "Source: drawn rectangle"
            )

        except Exception as e:
            QMessageBox.critical(
                None,
                "UrbanMorphoBox",
                f"Error:\n\n{str(e)}"
            )