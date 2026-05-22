# -*- coding: utf-8 -*-
"""
/***************************************************************************
 UrbanMorphoBox
                                 A QGIS plugin
 Download building footprints from OSM within a bounding box
 ***************************************************************************/
"""

from qgis.PyQt.QtCore import QSettings, QTranslator, QCoreApplication
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction
from .resources import *
from .urbanmorphobox_dialog import UrbanMorphoBoxDialog

import os.path


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

        import requests

        from qgis.PyQt.QtWidgets import QMessageBox

        from qgis.core import (
            QgsCoordinateReferenceSystem,
            QgsCoordinateTransform,
            QgsProject,
            QgsVectorLayer,
            QgsFeature,
            QgsGeometry,
            QgsPointXY,
            QgsField
        )

        from PyQt5.QtCore import QVariant

        max_features = 10000
        max_bbox_area_deg = 0.05

        canvas = self.iface.mapCanvas()
        extent = canvas.extent()

        source_crs = canvas.mapSettings().destinationCrs()
        target_crs = QgsCoordinateReferenceSystem("EPSG:4326")

        transform = QgsCoordinateTransform(
            source_crs,
            target_crs,
            QgsProject.instance()
        )

        extent_wgs84 = transform.transformBoundingBox(extent)

        west = extent_wgs84.xMinimum()
        east = extent_wgs84.xMaximum()
        south = extent_wgs84.yMinimum()
        north = extent_wgs84.yMaximum()

        bbox_area = abs((east - west) * (north - south))

        if bbox_area > max_bbox_area_deg:
            reply = QMessageBox.question(
                None,
                "UrbanMorphoBox",
                "The current map extent is quite large.\n\n"
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

            if response.status_code != 200:
                QMessageBox.warning(
                    None,
                    "UrbanMorphoBox",
                    f"Overpass request failed:\n{response.status_code}"
                )
                return

            data = response.json()

            layer = QgsVectorLayer(
                "Polygon?crs=EPSG:4326",
                "OSM Buildings",
                "memory"
            )

            provider = layer.dataProvider()

            provider.addAttributes([
                QgsField("osm_id", QVariant.String)
            ])

            layer.updateFields()

            features = []

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

                feature = QgsFeature()

                feature.setGeometry(
                    QgsGeometry.fromPolygonXY([points])
                )

                feature.setAttributes([
                    str(element["id"])
                ])

                features.append(feature)

            provider.addFeatures(features)
            layer.updateExtents()

            QgsProject.instance().addMapLayer(layer)

            QMessageBox.information(
                None,
                "UrbanMorphoBox",
                "Download finished.\n\n"
                f"Downloaded buildings: {len(features)}\n"
                f"Object limit: {max_features}\n\n"
                "Source: current map extent"
            )

        except Exception as e:
            QMessageBox.critical(
                None,
                "UrbanMorphoBox",
                f"Error:\n\n{str(e)}"
            )
