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
            QgsProject
        )

        # Current map canvas
        canvas = self.iface.mapCanvas()
        extent = canvas.extent()

        # Transform current CRS to WGS84
        source_crs = canvas.mapSettings().destinationCrs()
        target_crs = QgsCoordinateReferenceSystem("EPSG:4326")

        transform = QgsCoordinateTransform(
            source_crs,
            target_crs,
            QgsProject.instance()
        )

        extent_wgs84 = transform.transformBoundingBox(extent)

        # Extract coordinates
        west = extent_wgs84.xMinimum()
        east = extent_wgs84.xMaximum()
        south = extent_wgs84.yMinimum()
        north = extent_wgs84.yMaximum()

        # Overpass bounding box format
        bbox = f"{south},{west},{north},{east}"

        # Overpass query
        query = f"""
        [out:json][timeout:25];
        (
          way["building"]({bbox});
          relation["building"]({bbox});
        );
        out count;
        """

        # Overpass API endpoint
        url = "https://overpass-api.de/api/interpreter"

        # Request headers
        headers = {
            "User-Agent": "UrbanMorphoBox QGIS Plugin/0.1 (educational use)"
        }

        try:

            response = requests.post(
                url,
                data={"data": query},
                headers=headers,
                timeout=30
            )

            if response.status_code == 200:

                QMessageBox.information(
                    None,
                    "UrbanMorphoBox",
                    f"Overpass request successful.\n\n"
                    f"BBOX:\n{bbox}\n\n"
                    f"Response:\n{response.text[:500]}"
                )

            else:

                QMessageBox.warning(
                    None,
                    "UrbanMorphoBox",
                    f"Overpass request failed.\n\n"
                    f"Status: {response.status_code}\n\n"
                    f"{response.text[:500]}"
                )

        except Exception as e:

            QMessageBox.critical(
                None,
                "UrbanMorphoBox",
                f"Request error:\n\n{str(e)}"
            )