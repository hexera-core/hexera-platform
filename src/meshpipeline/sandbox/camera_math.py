# Responsibility: Do the camera arithmetic behind a review navigation command.
# Boundaries: pure geometry; it imports no renderer and holds no scene.
from __future__ import annotations

import math

import numpy as np

#: Below this, a vector is treated as having no direction.
EPS = 1e-12


def bounds_center_span(bounds: tuple[float, ...]) -> tuple[np.ndarray, float]:
    xmin, xmax, ymin, ymax, zmin, zmax = bounds
    center = np.array([(xmin + xmax) / 2, (ymin + ymax) / 2, (zmin + zmax) / 2])
    span = max(xmax - xmin, ymax - ymin, zmax - zmin, 1e-9)
    return center, span


def camera_right(position, focal_point, up) -> np.ndarray:
    fwd = np.array(focal_point) - np.array(position)
    right = np.cross(fwd, np.array(up))
    n = np.linalg.norm(right)
    return right / n if n > EPS else np.array([1.0, 0.0, 0.0])


def camera_true_up(position, focal_point, up) -> np.ndarray:
    fwd = np.array(focal_point) - np.array(position)
    fwd_n = fwd / max(np.linalg.norm(fwd), EPS)
    up_ortho = np.array(up) - np.dot(np.array(up), fwd_n) * fwd_n
    n = np.linalg.norm(up_ortho)
    return up_ortho / n if n > EPS else np.array([0.0, 0.0, 1.0])


def rodrigues(v: np.ndarray, k: np.ndarray, angle_rad: float) -> np.ndarray:
    k = k / max(np.linalg.norm(k), EPS)
    return (v * math.cos(angle_rad)
            + np.cross(k, v) * math.sin(angle_rad)
            + k * np.dot(k, v) * (1 - math.cos(angle_rad)))


__all__ = ["EPS", "bounds_center_span", "camera_right", "camera_true_up", "rodrigues"]
