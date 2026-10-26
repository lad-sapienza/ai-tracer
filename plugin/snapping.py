"""Topological snapping of a new polygon to the polygons already in a layer.

The new outline is made to *share* its boundary with nearby existing polygons,
tracing a single coherent perimeter whichever neighbours it runs along (like
QGIS's tracing tool):

1. Overlaps are cut away, so the new polygon never covers an existing one.
2. The outlines of all neighbours are noded into one network. Every edge of
   the new polygon whose two end points lie within the snap tolerance of that
   network is replaced by the shortest walk along the network between the two
   projected points. The walk may cross from one neighbour to the next where
   their outlines meet; where they do not, the two projected points are
   joined directly. A lone vertex near the network is left alone: the minimum
   contact is one segment.
3. Vertices of the new polygon that now sit on a neighbour's outline are
   inserted into that neighbour, so both outlines have coincident vertices
   (the neighbour's shape does not change).

If the result is not a valid, sensible polygon the step is skipped. The module
only depends on qgis.core geometry, so it can be tested without a running QGIS
application.
"""

import heapq
from dataclasses import dataclass, field

from qgis.core import Qgis, QgsGeometry

# A neighbour overlapping more than this share of the new polygon is probably
# the wrong object for the new one (or vice versa): leave it alone.
MAX_OVERLAP_SHARE = 0.5
# Snapping may not change the polygon's area by more than this share.
MAX_AREA_CHANGE = 0.5
# A replaced edge and the walk that replaces it must stay within this many
# tolerances of each other (Hausdorff distance).
MAX_PATH_DEVIATION = 2.0
# Routes longer than this multiple of the straight distance (plus a few
# tolerances) are not searched.
MAX_DETOUR = 3.0


@dataclass
class SnapResult:
    polygon: list                      # closed ring of QgsPointXY
    snapped_ids: list = field(default_factory=list)
    neighbor_updates: dict = field(default_factory=dict)   # id -> QgsGeometry
    shared_ratio: float = 0.0          # share of the perimeter on neighbours

    @property
    def changed(self) -> bool:
        return bool(self.snapped_ids)


def snap_polygon(ring: list, neighbors: dict, tolerance: float) -> SnapResult:
    """Snap *ring* (closed list of QgsPointXY) to *neighbors* ({id: QgsGeometry}).

    *tolerance* is in map units. Returns the original ring untouched when
    nothing could be snapped.
    """
    unchanged = SnapResult(polygon=ring)
    if tolerance <= 0 or len(ring) < 4 or not neighbors:
        return unchanged

    current = QgsGeometry.fromPolygonXY([ring])
    area0 = current.area()
    if current.isEmpty() or area0 <= 0:
        return unchanged

    included = {}
    for fid, geom in neighbors.items():
        if geom is None or geom.isEmpty() or current.distance(geom) > tolerance:
            continue
        overlap = current.intersection(geom)
        if not overlap.isEmpty():
            if overlap.area() > MAX_OVERLAP_SHARE * area0:
                continue
            if overlap.area() >= geom.area() * (1 - 1e-9):
                continue    # entirely inside the new polygon: not an edge
        included[fid] = geom
    if not included:
        return unchanged

    union = QgsGeometry.unaryUnion(list(included.values()))
    work = current
    overlap = current.intersection(union)
    if not overlap.isEmpty() and overlap.area() > 0:
        if overlap.area() > MAX_OVERLAP_SHARE * area0:
            return unchanged
        work = _largest_polygon(current.difference(union))
        if work is None:
            return unchanged

    network = _Network(
        [QgsGeometry.fromPolylineXY(r)
         for g in included.values() for r in _boundaries(g)]
    )
    traced = _trace_ring(_outer_ring(work), network, tolerance)
    if traced is not None:
        trial = _largest_polygon(QgsGeometry.fromPolygonXY([traced]))
        if trial is not None:
            # A walk around the far side of a neighbour can re-cover it.
            trial = _largest_polygon(trial.difference(union))
        if trial is not None and \
                abs(trial.area() - work.area()) <= MAX_AREA_CHANGE * work.area():
            work = trial

    if abs(work.area() - area0) > MAX_AREA_CHANGE * area0:
        return unchanged

    ring_pts = _outer_ring(work)
    boundary = QgsGeometry.fromPolylineXY(ring_pts)
    eps = max(tolerance * 1e-3, 1e-12)
    snapped, updates = [], {}
    for fid, geom in included.items():
        zone = _boundary_line(geom).buffer(tolerance * 0.05, 4)
        if boundary.intersection(zone).length() <= tolerance * 0.1:
            continue
        snapped.append(fid)
        new_geom = _insert_shared_vertices(geom, ring_pts, eps)
        if new_geom is not None:
            updates[fid] = new_geom

    if not snapped:
        return unchanged
    return SnapResult(
        polygon=ring_pts,
        snapped_ids=snapped,
        neighbor_updates=updates,
        shared_ratio=_shared_ratio(work, [included[f] for f in snapped],
                                   tolerance * 0.05),
    )


# --------------------------------------------------------------------------- #
# Outline network                                                             #
# --------------------------------------------------------------------------- #

class _Network:
    """The neighbours' outlines noded into edges between junction nodes."""

    def __init__(self, lines: list):
        merged = QgsGeometry.unaryUnion(lines)
        pieces = [part.asPolyline() for part in merged.asGeometryCollection()
                  if part.type() == Qgis.GeometryType.Line]
        self.pts = []        # per edge: vertices (QgsPointXY)
        self.cum = []        # per edge: arc position of each vertex
        self.line = []       # per edge: QgsGeometry
        self.ends = []       # per edge: (start node, end node)
        self.box = []        # per edge: bounding box
        self.adj = {}        # node -> [(edge index, other node, length)]
        for pts in pieces:
            if len(pts) < 2:
                continue
            cum = [0.0]
            for a, b in zip(pts, pts[1:]):
                cum.append(cum[-1] + a.distance(b))
            if cum[-1] <= 0:
                continue
            k = len(self.pts)
            u, v = _key(pts[0]), _key(pts[-1])
            self.pts.append(pts)
            self.cum.append(cum)
            line = QgsGeometry.fromPolylineXY(pts)
            self.line.append(line)
            self.box.append(line.boundingBox())
            self.ends.append((u, v))
            self.adj.setdefault(u, []).append((k, v, cum[-1]))
            if v != u:
                self.adj.setdefault(v, []).append((k, u, cum[-1]))

    def project(self, point, tol: float):
        """Nearest position on the network within *tol*: (edge, arc, point)."""
        pg = QgsGeometry.fromPointXY(point)
        best = None
        for k, box in enumerate(self.box):
            if not (box.xMinimum() - tol <= point.x() <= box.xMaximum() + tol
                    and box.yMinimum() - tol <= point.y() <= box.yMaximum() + tol):
                continue
            d = self.line[k].distance(pg)
            if d <= tol and (best is None or d < best[0]):
                best = (d, k)
        if best is None:
            return None
        k = best[1]
        s = self.line[k].lineLocatePoint(pg)
        return k, s, self.line[k].interpolate(s).asPoint()

    def _sub(self, k: int, s0: float, s1: float) -> list:
        """Points along edge *k* from arc position s0 to s1."""
        if s0 > s1:
            return list(reversed(self._sub(k, s1, s0)))
        mid = [p for p, c in zip(self.pts[k], self.cum[k]) if s0 < c < s1]
        return ([self.line[k].interpolate(s0).asPoint()] + mid
                + [self.line[k].interpolate(s1).asPoint()])

    def _edge_points(self, k: int, frm, to) -> list:
        pts = self.pts[k]
        return list(pts) if self.ends[k] == (frm, to) or frm == to \
            else list(reversed(pts))

    def route(self, a, b, cutoff: float):
        """Shortest walk from position *a* to *b* ((edge, arc) pairs), or None."""
        ea, sa = a
        eb, sb = b
        la, lb = self.cum[ea][-1], self.cum[eb][-1]
        best = (abs(sa - sb), None, None) if ea == eb else None

        dist, prev, heap = {}, {}, []
        ua, va = self.ends[ea]
        for node, cost, side in ((ua, sa, 0), (va, la - sa, 1)):
            if cost < dist.get(node, float("inf")):
                dist[node] = cost
                prev[node] = ("seed", side)
                heapq.heappush(heap, (cost, node))
        done = set()
        while heap:
            cost, node = heapq.heappop(heap)
            if node in done or cost > cutoff:
                continue
            done.add(node)
            for k, other, length in self.adj.get(node, ()):
                c = cost + length
                if c < dist.get(other, float("inf")):
                    dist[other] = c
                    prev[other] = (node, k)
                    heapq.heappush(heap, (c, other))

        ub, vb = self.ends[eb]
        for node, tail, side in ((ub, sb, 0), (vb, lb - sb, 1)):
            if node in done:
                total = dist[node] + tail
                if best is None or total < best[0]:
                    best = (total, node, side)
        if best is None or best[0] > cutoff:
            return None
        if best[1] is None:                       # same edge, straight along
            return self._sub(ea, sa, sb)

        _, node, tail_side = best
        hops = []
        while prev[node][0] != "seed":
            frm, k = prev[node]
            hops.append((k, frm, node))
            node = frm
        path = self._sub(ea, sa, 0.0 if prev[node][1] == 0 else la)
        for k, frm, to in reversed(hops):
            path += self._edge_points(k, frm, to)
        path += self._sub(eb, 0.0 if tail_side == 0 else lb, sb)
        return _dedupe(path, 0.0, keep_ring=False)


def _trace_ring(ring_pts: list, net: _Network, tol: float):
    """Replace edges running along the network by walks on the network.

    Returns the new closed ring, or None when no edge was replaced.
    """
    pts = ring_pts[:-1] if ring_pts[0] == ring_pts[-1] else list(ring_pts)
    n = len(pts)
    if n < 3 or not net.pts:
        return None

    proj = [net.project(p, tol) for p in pts]
    replacement = [None] * n
    for i in range(n):
        j = (i + 1) % n
        if proj[i] is None or proj[j] is None:
            continue
        chord = pts[i].distance(pts[j])
        route = net.route(proj[i][:2], proj[j][:2],
                          MAX_DETOUR * chord + 6 * tol)
        edge = QgsGeometry.fromPolylineXY([pts[i], pts[j]])
        if route is not None and len(route) >= 2 and \
                edge.hausdorffDistance(QgsGeometry.fromPolylineXY(route)) \
                <= MAX_PATH_DEVIATION * tol:
            replacement[i] = route
        else:
            # No usable walk (outlines that do not meet): join the projected
            # points directly.
            replacement[i] = [proj[i][2], proj[j][2]]

    if not any(replacement):
        return None

    out = []
    for i in range(n):
        nxt, prv = replacement[i], replacement[i - 1]
        if nxt is not None:
            out.extend(nxt)
        elif prv is not None:
            out.append(prv[-1])
        else:
            out.append(pts[i])

    out = _dedupe(out, tol * 1e-3)
    if len(out) < 3:
        return None
    out.append(out[0])
    return out


# --------------------------------------------------------------------------- #
# Vertices shared with the neighbour                                          #
# --------------------------------------------------------------------------- #

def _insert_shared_vertices(neighbor: QgsGeometry, ring_pts: list, eps: float):
    """Copy of *neighbor* with the ring's vertices lying on it inserted.

    Returns None when the neighbour already has them all.
    """
    geom = QgsGeometry(neighbor)
    added = False
    for p in ring_pts[:-1]:
        vertex_sqr, vertex_idx = geom.closestVertexWithContext(p)
        if vertex_idx >= 0 and vertex_sqr <= eps * eps:
            continue    # already a vertex of the neighbour
        sqr_dist, _, after, _ = geom.closestSegmentWithContext(p)
        if sqr_dist <= eps * eps and after > 0:
            if geom.insertVertex(p.x(), p.y(), after):
                added = True
    return geom if added else None


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _key(p) -> tuple:
    return (p.x(), p.y())


def _polygons(geom: QgsGeometry) -> list:
    """Polygons (lists of rings) in *geom*, ignoring non-polygon parts.

    Works for single and multi polygons and for the geometry collections that
    makeValid() / difference() may return.
    """
    if geom.isEmpty():
        return []
    if geom.isMultipart():
        # A collection can nest multi-part geometries, hence the recursion.
        return [poly for part in geom.asGeometryCollection()
                for poly in _polygons(part)]
    if geom.type() != Qgis.GeometryType.Polygon:
        return []
    poly = geom.asPolygon()
    return [poly] if poly else []


def _boundaries(geom: QgsGeometry) -> list:
    return [ring for poly in _polygons(geom) for ring in poly]


def _boundary_line(geom: QgsGeometry) -> QgsGeometry:
    return QgsGeometry.fromMultiPolylineXY(_boundaries(geom))


def _outer_ring(geom: QgsGeometry) -> list:
    polys = _polygons(geom)
    return polys[0][0] if polys else []


def _largest_polygon(geom: QgsGeometry):
    """Largest valid polygon part of *geom*, or None.

    None as well when that part has holes: dropping them would silently
    re-cover whatever the holes were cut out for.
    """
    if not geom.isGeosValid():
        geom = geom.makeValid()
    polys = _polygons(geom)
    if not polys:
        return None
    best = max(polys, key=lambda p: QgsGeometry.fromPolygonXY(p).area())
    if len(best) > 1:
        return None
    result = QgsGeometry.fromPolygonXY(best)
    if result.isEmpty() or result.area() <= 0:
        return None
    return result


def _dedupe(pts: list, eps: float, keep_ring: bool = True) -> list:
    """Drop consecutive repeated points (and, for a ring, a repeated end)."""
    out = []
    for p in pts:
        if not out or out[-1].distance(p) > eps:
            out.append(p)
    while keep_ring and len(out) > 1 and out[0].distance(out[-1]) <= eps:
        out.pop()
    return out


def _shared_ratio(poly: QgsGeometry, neighbors: list, tol: float) -> float:
    boundary = QgsGeometry.fromPolylineXY(_outer_ring(poly))
    length = boundary.length()
    if length <= 0:
        return 0.0
    shared = 0.0
    for n in neighbors:
        zone = _boundary_line(n).buffer(tol, 4)
        shared += boundary.intersection(zone).length()
    return min(1.0, shared / length)
