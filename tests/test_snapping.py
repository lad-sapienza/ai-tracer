"""Tests for plugin/snapping.py. Run with QGIS's Python:

    /Applications/QGIS-final-4_2_3.app/Contents/MacOS/python -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugin"))

from qgis.core import QgsGeometry, QgsPointXY  # noqa: E402

from snapping import _boundary_line as boundary, snap_polygon  # noqa: E402


def ring(*xy):
    pts = [QgsPointXY(x, y) for x, y in xy]
    return pts + [pts[0]]


def poly(*xy):
    return QgsGeometry.fromPolygonXY([ring(*xy)])


def verts(geom):
    return {(round(p.x(), 9), round(p.y(), 9))
            for r in geom.asPolygon() for p in r}


TOL = 0.1


class SnapTests(unittest.TestCase):
    def setUp(self):
        self.neighbor = poly((0, 0), (10, 0), (10, 10), (0, 10))

    def snap(self, new_ring, neighbors=None, tol=TOL):
        return snap_polygon(new_ring, neighbors or {1: self.neighbor}, tol)

    def assertTopology(self, res, neighbor):
        new = QgsGeometry.fromPolygonXY([res.polygon])
        self.assertTrue(new.isGeosValid())
        self.assertAlmostEqual(new.intersection(neighbor).area(), 0, places=9)
        # Every vertex of the new polygon lying on the neighbour's boundary
        # must be a vertex of the (updated) neighbour.
        updated = res.neighbor_updates.get(1, neighbor)
        nv = verts(updated)
        for p in res.polygon:
            if boundary(updated).distance(QgsGeometry.fromPointXY(p)) < 1e-9:
                self.assertIn((round(p.x(), 9), round(p.y(), 9)), nv)

    def test_gap_is_closed(self):
        # New stone right of the neighbour, 0.05 away from its edge x=10.
        res = self.snap(ring((10.05, 2), (15, 2), (15, 8), (10.05, 8)))
        self.assertTrue(res.changed)
        new = QgsGeometry.fromPolygonXY([res.polygon])
        shared = boundary(new).intersection(boundary(self.neighbor))
        self.assertAlmostEqual(shared.length(), 6.0, places=6)
        self.assertTopology(res, self.neighbor)
        self.assertIn((10.0, 2.0), verts(res.neighbor_updates[1]))
        self.assertIn((10.0, 8.0), verts(res.neighbor_updates[1]))
        self.assertAlmostEqual(res.shared_ratio, 6 / 22, places=2)

    def test_overlap_is_cut(self):
        res = self.snap(ring((9.95, 2), (15, 2), (15, 8), (9.95, 8)))
        self.assertTrue(res.changed)
        self.assertTopology(res, self.neighbor)
        new = QgsGeometry.fromPolygonXY([res.polygon])
        self.assertAlmostEqual(
            boundary(new).intersection(boundary(self.neighbor)).length(),
            6.0, places=6)

    def test_beyond_tolerance_untouched(self):
        r = ring((10.5, 2), (15, 2), (15, 8), (10.5, 8))
        res = self.snap(r)
        self.assertFalse(res.changed)
        self.assertEqual(res.polygon, r)
        self.assertEqual(res.neighbor_updates, {})

    def test_zero_tolerance_disabled(self):
        r = ring((10.05, 2), (15, 2), (15, 8), (10.05, 8))
        self.assertFalse(self.snap(r, tol=0).changed)

    def test_single_vertex_is_not_snapped(self):
        # Only one vertex within tolerance: contact shorter than a segment.
        r = ring((10.05, 5), (14, 1), (15, 8))
        res = self.snap(r)
        self.assertFalse(res.changed)
        self.assertEqual(res.polygon, r)

    def test_follows_a_corner(self):
        # L-shaped neighbour; new stone sits in the notch, 0.05 off both walls.
        l_shape = poly((0, 0), (10, 0), (10, 4), (4, 4), (4, 10), (0, 10))
        r = ring((4.05, 4.05), (9.95, 4.05), (9.95, 9), (4.05, 9))
        res = snap_polygon(r, {1: l_shape}, TOL)
        self.assertTrue(res.changed)
        new = QgsGeometry.fromPolygonXY([res.polygon])
        self.assertTrue(new.isGeosValid())
        self.assertAlmostEqual(new.intersection(l_shape).area(), 0, places=9)
        shared = boundary(new).intersection(boundary(l_shape)).length()
        self.assertGreater(shared, 10.9)   # 5.95 + 5 along the two walls
        self.assertIn((4.0, 4.0), verts(new))

    def test_two_neighbours(self):
        right = poly((10.05, 0), (20, 0), (20, 10), (10.05, 10))
        # New stone straddling the 0.05 slot between two stones.
        new = ring((9.98, 3), (10.07, 3), (10.07, 7), (9.98, 7))
        res = snap_polygon(new, {1: self.neighbor, 2: right}, TOL)
        poly_geom = QgsGeometry.fromPolygonXY([res.polygon])
        self.assertTrue(poly_geom.isGeosValid())
        self.assertAlmostEqual(poly_geom.intersection(self.neighbor).area(), 0, places=9)
        self.assertAlmostEqual(poly_geom.intersection(right).area(), 0, places=9)
        self.assertEqual(sorted(res.snapped_ids), [1, 2])

    def test_huge_overlap_skipped(self):
        r = ring((2, 2), (8, 2), (8, 8), (2, 8))   # fully inside the neighbour
        res = self.snap(r)
        self.assertFalse(res.changed)

    def test_noisy_contour_collapses_onto_boundary(self):
        # Many vertices wobbling +-0.04 around the neighbour's edge x=10.
        pts = [(10 + (0.04 if i % 2 else -0.04) * (i % 3 - 1 or 1), y)
               for i, y in enumerate(range(1, 10))]
        r = ring(*pts, (16, 9), (16, 1))
        res = self.snap(r)
        self.assertTrue(res.changed)
        new = QgsGeometry.fromPolygonXY([res.polygon])
        self.assertTrue(new.isGeosValid())
        self.assertAlmostEqual(new.intersection(self.neighbor).area(), 0, places=9)
        shared = boundary(new).intersection(boundary(self.neighbor)).length()
        self.assertAlmostEqual(shared, 8.0, places=6)

    def test_traces_across_touching_neighbours(self):
        # Two stones side by side; the new one below spans both, as in the
        # case where an outline used to follow only one of its neighbours.
        a = poly((0, 0), (10, 0), (10, 10), (0, 10))
        b = poly((10, 0), (20, 0), (20, 10), (10, 10))
        res = snap_polygon(
            ring((4, -0.05), (16, -0.05), (16, -5), (4, -5)),
            {1: a, 2: b}, TOL)
        self.assertEqual(sorted(res.snapped_ids), [1, 2])
        new = QgsGeometry.fromPolygonXY([res.polygon])
        self.assertTrue(new.isGeosValid())
        self.assertAlmostEqual(new.intersection(a).area(), 0, places=9)
        self.assertAlmostEqual(new.intersection(b).area(), 0, places=9)
        shared = (boundary(new).intersection(boundary(a)).length()
                  + boundary(new).intersection(boundary(b)).length())
        self.assertAlmostEqual(shared, 12.0, places=6)
        self.assertIn((10.0, 0.0), verts(new))
        self.assertIn((4.0, 0.0), verts(res.neighbor_updates[1]))
        self.assertIn((16.0, 0.0), verts(res.neighbor_updates[2]))

    def test_bridges_a_gap_between_neighbours(self):
        a = poly((0, 0), (10, 0), (10, 10), (0, 10))
        b = poly((10.04, 0), (20, 0), (20, 10), (10.04, 10))
        res = snap_polygon(
            ring((4, -0.05), (16, -0.05), (16, -5), (4, -5)),
            {1: a, 2: b}, TOL)
        self.assertEqual(sorted(res.snapped_ids), [1, 2])
        new = QgsGeometry.fromPolygonXY([res.polygon])
        self.assertTrue(new.isGeosValid())
        self.assertAlmostEqual(new.intersection(a).area(), 0, places=9)
        self.assertAlmostEqual(new.intersection(b).area(), 0, places=9)

    def test_surrounded_by_three_neighbours(self):
        # Notch between three stones; the outline must hug all of them.
        left = poly((0, 0), (5, 0), (5, 10), (0, 10))
        right = poly((10, 0), (15, 0), (15, 10), (10, 10))
        top = poly((5, 8), (10, 8), (10, 10), (5, 10))
        new = ring((5.04, 0.5), (9.96, 0.5), (9.96, 7.96), (5.04, 7.96))
        res = snap_polygon(new, {1: left, 2: right, 3: top}, TOL)
        self.assertEqual(sorted(res.snapped_ids), [1, 2, 3])
        poly_geom = QgsGeometry.fromPolygonXY([res.polygon])
        self.assertTrue(poly_geom.isGeosValid())
        for n in (left, right, top):
            self.assertAlmostEqual(poly_geom.intersection(n).area(), 0, places=9)
        # Fills the notch x 5..10, y 0.5..8 almost entirely.
        self.assertAlmostEqual(poly_geom.area(), 5 * 7.5, delta=0.01)

    def test_geometry_collection_from_make_valid(self):
        # A bow-tie ring makes makeValid() return a multi-part/collection.
        bow = ring((10.05, 2), (15, 8), (15, 2), (10.05, 8))
        self.snap(bow)   # must not raise

    def test_neighbour_shape_unchanged(self):
        res = self.snap(ring((10.05, 2), (15, 2), (15, 8), (10.05, 8)))
        updated = res.neighbor_updates[1]
        self.assertAlmostEqual(updated.area(), self.neighbor.area(), places=9)
        self.assertTrue(updated.isGeosValid())


if __name__ == "__main__":
    unittest.main()
