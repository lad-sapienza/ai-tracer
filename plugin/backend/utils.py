import numpy as np
import cv2


def mask_to_polygon_at_point(mask: np.ndarray, click_xy: list) -> list:
    """Extract the polygon from a binary mask that contains the click point.

    Selects the contour that contains click_xy (first positive prompt point).
    Falls back to the contour nearest to click_xy if none contains it.
    Returns a list of [x, y] integer pairs (closed ring), or [] if no contour found.
    """
    if mask.dtype != np.uint8:
        mask = (mask > 0).astype(np.uint8) * 255

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return []

    point = (float(click_xy[0]), float(click_xy[1]))

    # Prefer the contour that contains the click point
    containing = [
        c for c in contours
        if cv2.pointPolygonTest(c, point, False) >= 0
    ]
    if containing:
        # If multiple contain the point (shouldn't happen with RETR_EXTERNAL),
        # take the smallest — it's the tightest fit around the clicked object
        best = min(containing, key=cv2.contourArea)
    else:
        # Fallback: contour whose centroid is nearest to the click point
        def centroid_dist(c):
            M = cv2.moments(c)
            if M["m00"] == 0:
                return float("inf")
            cx = M["m10"] / M["m00"]
            cy = M["m01"] / M["m00"]
            return (cx - point[0]) ** 2 + (cy - point[1]) ** 2

        best = min(contours, key=centroid_dist)

    if cv2.contourArea(best) == 0:
        return []

    points = best.squeeze(axis=1).tolist()
    points.append(points[0])  # close the ring
    return points
