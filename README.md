# UrbanMorphoBox

QGIS plugin for downloading and analysing OpenStreetMap (OSM) building footprints for urban morphology studies.

## Status

Experimental (v0.1)

UrbanMorphoBox is an early-stage QGIS plugin currently under active development. The plugin is functional but should be considered experimental. Features, interfaces and workflows may change in future versions.

## Features

Current functionality includes:

* Draw a rectangle directly on the QGIS map canvas
* Download OSM building footprints from the selected area using the Overpass API
* Current limit: 10,000 buildings
* Calculate building area (m²)
* Calculate average building size
* Calculate building density (buildings/km²)
* Building count statistics
* Bounding box size warning
* Basic Overpass API error handling

## Planned Features

* Area-based symbology
* GeoPackage export
* Large-area tiling for extensive downloads
* Additional urban morphology metrics
* Improved Overpass API handling

## Installation

Currently available through GitHub.

Clone or download the repository and copy the plugin folder to your QGIS plugin directory.

## Author

Harald Schernthanner

## License

GPL-3.0
