from qgis.core import QgsPointXY, QgsMapToPixel, QgsGeometry


def pixel_to_geo(px: float, py: float,
                 mtp: QgsMapToPixel,
                 dpr: float = 1.0) -> QgsPointXY:
    """Convert physical canvas pixel coordinates to map CRS coordinates.

    Uses QgsMapToPixel so canvas rotation is handled correctly.
    dpr: device pixel ratio (>1 on HiDPI/Retina displays).
    """
    # mtp works in logical pixels; divide physical coords by dpr first
    return mtp.toMapCoordinates(int(px / dpr), int(py / dpr))


def geo_to_pixel(point: QgsPointXY,
                 mtp: QgsMapToPixel,
                 dpr: float = 1.0) -> tuple:
    """Convert map CRS coordinates to physical canvas pixel coordinates."""
    pt = mtp.transform(point)
    return (int(pt.x() * dpr), int(pt.y() * dpr))


def polygon_pixel_to_geo(pixel_polygon: list,
                         mtp: QgsMapToPixel,
                         dpr: float = 1.0) -> list:
    """Convert a list of [x, y] physical pixel pairs to QgsPointXY geo coordinates."""
    return [pixel_to_geo(p[0], p[1], mtp, dpr) for p in pixel_polygon]


def simplify_polygon_geo(polygon_geo: list, tolerance: float) -> list:
    """Simplify a polygon (list of QgsPointXY) using Douglas-Peucker.

    Returns the simplified list of QgsPointXY. If tolerance is 0 or
    simplification produces a degenerate result, the original is returned.
    """
    if tolerance <= 0 or len(polygon_geo) < 4:
        return polygon_geo
    geom = QgsGeometry.fromPolygonXY([polygon_geo])
    simplified = geom.simplify(tolerance)
    if simplified.isEmpty():
        return polygon_geo
    ring = simplified.asPolygon()
    if not ring:
        return polygon_geo
    return ring[0]
