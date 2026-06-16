"""Classical fingerprint feature-extraction research pipeline.

This module reconstructs the useful parts of the legacy notebook into an
ordered, testable pipeline.  It is intentionally a vision research prototype,
not a production AFIS implementation and not an ISO template encoder.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, Sequence
import csv
import json
import math

import cv2
import numpy as np
from scipy import ndimage
from scipy.signal import find_peaks
from skimage.morphology import remove_small_objects, skeletonize


@dataclass(frozen=True)
class PipelineConfig:
    block_size: int = 16
    clahe_clip_limit: float = 2.0
    clahe_grid: tuple[int, int] = (8, 8)
    orientation_smooth_sigma_blocks: float = 1.0
    roi_dark_threshold: int = 248
    roi_close_size: int = 19
    roi_erode_px: int = 25
    adaptive_block_size: int = 31  # retained only for the documented experimental branch
    adaptive_c: int = 7
    morph_kernel_size: int = 1
    min_component_size: int = 24
    prune_length: int = 5
    minutia_roi_distance: float = 38.0
    minutia_min_coherence: float = 0.40
    minutia_min_separation: float = 20.0
    branch_trace_length: int = 18
    branch_min_length: int = 15
    broken_ridge_pair_max_gap: float = 24.0
    broken_ridge_pair_angle_tolerance_deg: float = 35.0
    singular_search_radius_px: int = 10
    singular_ring_radius_px: int = 10
    singular_min_coherence: float = 0.12


@dataclass
class Minutia:
    x: int
    y: int
    kind: str
    branch_angles_rad: list[float]
    branch_lengths_px: list[int]
    coherence: float
    roi_distance_px: float
    quality_proxy: float

    @property
    def orientation_rad(self) -> float | None:
        return self.branch_angles_rad[0] if self.branch_angles_rad else None


@dataclass
class SingularPoint:
    x: int
    y: int
    kind: str
    poincare_index_rad: float
    branch_angles_rad: list[float]
    coherence: float


@dataclass
class PipelineResult:
    original: np.ndarray
    normalized: np.ndarray
    enhanced: np.ndarray
    roi_mask: np.ndarray
    inner_roi_mask: np.ndarray
    orientation_blocks: np.ndarray
    coherence_blocks: np.ndarray
    orientation_dense: np.ndarray
    coherence_dense: np.ndarray
    binary_ridges: np.ndarray
    skeleton: np.ndarray
    minutiae: list[Minutia]
    singular_points: list[SingularPoint]
    diagnostics: dict


_OFFSETS_8 = [
    (-1, 0), (-1, 1), (0, 1), (1, 1),
    (1, 0), (1, -1), (0, -1), (-1, -1),
]


def load_grayscale(path: str | Path) -> np.ndarray:
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise FileNotFoundError(f"Could not read image: {path}")
    if img.ndim != 2:
        raise ValueError("Expected a single-channel grayscale image")
    return img


def normalize_and_clahe(img: np.ndarray, cfg: PipelineConfig) -> tuple[np.ndarray, np.ndarray]:
    normalized = cv2.normalize(img, None, 0, 255, cv2.NORM_MINMAX)
    clahe = cv2.createCLAHE(
        clipLimit=cfg.clahe_clip_limit,
        tileGridSize=cfg.clahe_grid,
    )
    enhanced = clahe.apply(normalized)
    return normalized, enhanced


def largest_component(mask: np.ndarray) -> np.ndarray:
    mask_u8 = (mask > 0).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask_u8, 8)
    if n <= 1:
        return mask_u8 * 255
    idx = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    return (labels == idx).astype(np.uint8) * 255


def compute_roi_mask(img: np.ndarray, cfg: PipelineConfig) -> tuple[np.ndarray, np.ndarray]:
    # The supplied laboratory image has a white background.  Thresholding dark
    # pixels, joining ridge fragments, and filling the largest contour yields a
    # stable foreground envelope without pretending to be a general segmenter.
    dark = (img < cfg.roi_dark_threshold).astype(np.uint8) * 255
    k = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (cfg.roi_close_size, cfg.roi_close_size)
    )
    connected = cv2.morphologyEx(dark, cv2.MORPH_CLOSE, k, iterations=2)
    connected = cv2.dilate(connected, k, iterations=1)
    connected = largest_component(connected)
    contours, _ = cv2.findContours(connected, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    filled = np.zeros_like(img, dtype=np.uint8)
    if contours:
        cnt = max(contours, key=cv2.contourArea)
        hull = cv2.convexHull(cnt)
        cv2.drawContours(filled, [hull], -1, 255, thickness=-1)
    else:
        filled[:] = 255
    # Smooth blocky edges and keep a conservative interior for minutiae.
    filled = cv2.morphologyEx(
        filled,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9)),
    )
    erode_k = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE,
        (2 * cfg.roi_erode_px + 1, 2 * cfg.roi_erode_px + 1),
    )
    inner = cv2.erode(filled, erode_k)
    return filled, inner


def _smooth_axial_field(theta: np.ndarray, sigma: float, mask: np.ndarray | None = None) -> np.ndarray:
    c = np.cos(2.0 * theta)
    s = np.sin(2.0 * theta)
    if mask is None:
        c_s = ndimage.gaussian_filter(c, sigma=sigma, mode="nearest")
        s_s = ndimage.gaussian_filter(s, sigma=sigma, mode="nearest")
    else:
        m = mask.astype(float)
        den = ndimage.gaussian_filter(m, sigma=sigma, mode="nearest") + 1e-8
        c_s = ndimage.gaussian_filter(c * m, sigma=sigma, mode="nearest") / den
        s_s = ndimage.gaussian_filter(s * m, sigma=sigma, mode="nearest") / den
    return 0.5 * np.arctan2(s_s, c_s)


def estimate_orientation_fields(
    img: np.ndarray,
    roi_mask: np.ndarray,
    cfg: PipelineConfig,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    f = img.astype(np.float32)
    gx = cv2.Sobel(f, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(f, cv2.CV_32F, 0, 1, ksize=3)
    gxx = gx * gx
    gyy = gy * gy
    gxy = gx * gy

    # Dense structure tensor for pixel-level refinement.
    sigma_px = max(3.0, cfg.block_size / 2.0)
    jxx = ndimage.gaussian_filter(gxx, sigma=sigma_px, mode="nearest")
    jyy = ndimage.gaussian_filter(gyy, sigma=sigma_px, mode="nearest")
    jxy = ndimage.gaussian_filter(gxy, sigma=sigma_px, mode="nearest")
    gradient_axis_dense = 0.5 * np.arctan2(2.0 * jxy, jxx - jyy)
    ridge_dense = np.mod(gradient_axis_dense + np.pi / 2.0, np.pi)
    coherence_dense = np.sqrt((jxx - jyy) ** 2 + 4.0 * jxy ** 2) / (jxx + jyy + 1e-8)
    ridge_dense = _smooth_axial_field(
        ridge_dense,
        sigma=max(1.0, cfg.block_size / 8.0),
        mask=(roi_mask > 0),
    )

    h, w = img.shape
    bs = cfg.block_size
    bh = math.ceil(h / bs)
    bw = math.ceil(w / bs)
    theta_b = np.zeros((bh, bw), dtype=np.float32)
    coherence_b = np.zeros((bh, bw), dtype=np.float32)
    block_mask = np.zeros((bh, bw), dtype=np.uint8)
    for br in range(bh):
        y0, y1 = br * bs, min((br + 1) * bs, h)
        for bc in range(bw):
            x0, x1 = bc * bs, min((bc + 1) * bs, w)
            roi_fraction = float(np.mean(roi_mask[y0:y1, x0:x1] > 0))
            if roi_fraction < 0.25:
                continue
            sx = float(np.sum(gxx[y0:y1, x0:x1]))
            sy = float(np.sum(gyy[y0:y1, x0:x1]))
            sxy = float(np.sum(gxy[y0:y1, x0:x1]))
            gradient_axis = 0.5 * math.atan2(2.0 * sxy, sx - sy)
            theta_b[br, bc] = (gradient_axis + math.pi / 2.0) % math.pi
            coherence_b[br, bc] = math.sqrt((sx - sy) ** 2 + 4.0 * sxy ** 2) / (sx + sy + 1e-8)
            block_mask[br, bc] = 1
    theta_b = _smooth_axial_field(
        theta_b,
        sigma=cfg.orientation_smooth_sigma_blocks,
        mask=block_mask,
    ).astype(np.float32)
    coherence_b = ndimage.gaussian_filter(coherence_b, sigma=0.8)
    coherence_b *= block_mask
    return theta_b, coherence_b, ridge_dense.astype(np.float32), coherence_dense.astype(np.float32)


def binarize_and_skeletonize(
    enhanced: np.ndarray,
    roi_mask: np.ndarray,
    cfg: PipelineConfig,
) -> tuple[np.ndarray, np.ndarray, dict]:
    # The useful skeleton at the bottom of the legacy notebook came from a
    # direct binary ridge image followed by thinning.  For this already
    # high-contrast laboratory input, global Otsu thresholding preserves ridge
    # continuity better than the abandoned pixel-wise Gabor/adaptive branch.
    otsu_threshold, binary = cv2.threshold(
        enhanced, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
    )
    binary = cv2.bitwise_and(binary, roi_mask)
    if cfg.morph_kernel_size > 1:
        k = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (cfg.morph_kernel_size, cfg.morph_kernel_size),
        )
        binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, k, iterations=1)
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, k, iterations=1)
    clean_bool = remove_small_objects(
        binary > 0, max_size=max(0, cfg.min_component_size - 1)
    )
    skel_bool = skeletonize(clean_bool)
    skeleton = (skel_bool.astype(np.uint8) * 255)
    skeleton, pruned_pixels, prune_iterations = prune_short_endpoint_branches(
        skeleton, cfg.prune_length
    )
    diag = {
        "otsu_threshold": float(otsu_threshold),
        "binary_foreground_pixels": int(np.count_nonzero(binary)),
        "skeleton_pixels": int(np.count_nonzero(skeleton)),
        "pruned_skeleton_pixels": int(pruned_pixels),
        "prune_iterations": int(prune_iterations),
    }
    return binary, skeleton, diag


def neighbors_on_skeleton(skeleton_bool: np.ndarray, y: int, x: int) -> list[tuple[int, int]]:
    h, w = skeleton_bool.shape
    out: list[tuple[int, int]] = []
    for dy, dx in _OFFSETS_8:
        yy, xx = y + dy, x + dx
        if 0 <= yy < h and 0 <= xx < w and skeleton_bool[yy, xx]:
            out.append((yy, xx))
    return out


def crossing_number_at(skeleton_bool: np.ndarray, y: int, x: int) -> int:
    vals = []
    h, w = skeleton_bool.shape
    for dy, dx in _OFFSETS_8:
        yy, xx = y + dy, x + dx
        vals.append(1 if 0 <= yy < h and 0 <= xx < w and skeleton_bool[yy, xx] else 0)
    return sum(abs(vals[i] - vals[(i + 1) % 8]) for i in range(8)) // 2


def prune_short_endpoint_branches(skeleton: np.ndarray, max_length: int) -> tuple[np.ndarray, int, int]:
    sk = skeleton > 0
    removed_total = 0
    iterations = 0
    # A few rounds remove tiny thorns created by threshold noise while preserving
    # longer legitimate endings.  This is deliberately conservative.
    for _ in range(4):
        endpoints = [(y, x) for y, x in zip(*np.nonzero(sk)) if crossing_number_at(sk, y, x) == 1]
        to_remove: set[tuple[int, int]] = set()
        for ep in endpoints:
            path = [ep]
            prev = None
            cur = ep
            for _step in range(max_length + 1):
                nbrs = [p for p in neighbors_on_skeleton(sk, *cur) if p != prev]
                if len(nbrs) == 0:
                    break
                if len(nbrs) > 1:
                    break
                nxt = nbrs[0]
                path.append(nxt)
                prev, cur = cur, nxt
                cn = crossing_number_at(sk, *cur)
                if cn != 2:
                    break
            terminal_cn = crossing_number_at(sk, *cur)
            if len(path) - 1 <= max_length and terminal_cn >= 3:
                to_remove.update(path[:-1])
        if not to_remove:
            break
        for y, x in to_remove:
            sk[y, x] = False
        removed_total += len(to_remove)
        iterations += 1
    return (sk.astype(np.uint8) * 255), removed_total, iterations


def trace_branch(
    skeleton_bool: np.ndarray,
    origin: tuple[int, int],
    start: tuple[int, int],
    max_steps: int,
) -> list[tuple[int, int]]:
    path = [origin, start]
    visited = {origin, start}
    prev = origin
    cur = start
    prev_vec = np.array([start[1] - origin[1], start[0] - origin[0]], dtype=float)
    for _ in range(max_steps - 1):
        candidates = [p for p in neighbors_on_skeleton(skeleton_bool, *cur) if p not in visited]
        if not candidates:
            break
        # Choose the smoothest continuation.  At a junction this keeps the
        # traced arm stable instead of hopping to a side branch.
        scored = []
        for p in candidates:
            vec = np.array([p[1] - cur[1], p[0] - cur[0]], dtype=float)
            score = float(np.dot(prev_vec, vec) / ((np.linalg.norm(prev_vec) * np.linalg.norm(vec)) + 1e-8))
            scored.append((score, p, vec))
        scored.sort(key=lambda z: z[0], reverse=True)
        _, nxt, vec = scored[0]
        path.append(nxt)
        visited.add(nxt)
        prev, cur = cur, nxt
        prev_vec = vec
    return path


def _deduplicate_neighbor_starts(origin: tuple[int, int], starts: list[tuple[int, int]]) -> list[tuple[int, int]]:
    # Diagonally adjacent skeleton pixels can represent one physical branch.
    # Group starts by polar angle and retain separated directions.
    if len(starts) <= 1:
        return starts
    oy, ox = origin
    angle_items = []
    for p in starts:
        a = math.atan2(p[0] - oy, p[1] - ox) % (2 * math.pi)
        angle_items.append((a, p))
    angle_items.sort()
    selected: list[tuple[float, tuple[int, int]]] = []
    for a, p in angle_items:
        if all(min(abs(a - b), 2 * math.pi - abs(a - b)) > math.radians(35) for b, _ in selected):
            selected.append((a, p))
    return [p for _, p in selected]


def branch_descriptors(
    skeleton_bool: np.ndarray,
    y: int,
    x: int,
    max_steps: int,
) -> tuple[list[float], list[int]]:
    """Estimate physical branch arms after collapsing a multi-pixel junction.

    A skeleton bifurcation is rarely represented by one ideal pixel.  Removing
    a small junction disk and finding the connected components that leave that
    disk gives one descriptor per physical arm and avoids duplicate rays.
    """
    h, w = skeleton_bool.shape
    pad = max_steps + 4
    y0, y1 = max(0, y - pad), min(h, y + pad + 1)
    x0, x1 = max(0, x - pad), min(w, x + pad + 1)
    local = skeleton_bool[y0:y1, x0:x1].copy()
    oy, ox = y - y0, x - x0
    yy, xx = np.indices(local.shape)
    junction_radius = 2.5
    zone = local & (((yy - oy) ** 2 + (xx - ox) ** 2) <= junction_radius ** 2)
    remainder = local & ~zone
    n, labels = cv2.connectedComponents(remainder.astype(np.uint8), 8)
    zone_dilated = cv2.dilate(zone.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
    desc: list[tuple[float, int]] = []
    for label in range(1, n):
        comp = labels == label
        boundary = comp & zone_dilated
        if not np.any(boundary):
            continue
        by, bx = np.nonzero(boundary)
        # Start at the boundary pixel nearest the nominal minutia.
        k = int(np.argmin((by - oy) ** 2 + (bx - ox) ** 2))
        start_local = (int(by[k]), int(bx[k]))
        # Breadth-first geodesic distance inside this branch component.
        queue = [start_local]
        dist = {start_local: 0}
        qpos = 0
        while qpos < len(queue):
            cy, cx = queue[qpos]
            qpos += 1
            if dist[(cy, cx)] >= max_steps:
                continue
            for dy, dx in _OFFSETS_8:
                ny, nx = cy + dy, cx + dx
                if 0 <= ny < comp.shape[0] and 0 <= nx < comp.shape[1] and comp[ny, nx]:
                    if (ny, nx) not in dist:
                        dist[(ny, nx)] = dist[(cy, cx)] + 1
                        queue.append((ny, nx))
        end_local, length = max(dist.items(), key=lambda item: item[1])
        ey, ex = end_local
        angle = math.atan2((ey + y0) - y, (ex + x0) - x) % (2 * math.pi)
        desc.append((angle, int(length)))
    desc.sort(key=lambda z: z[0])
    return [a for a, _ in desc], [l for _, l in desc]


def branch_descriptors_for_candidate_cluster(
    skeleton_bool: np.ndarray,
    cluster: list[tuple[int, int, str]],
    max_steps: int,
) -> tuple[int, int, list[float], list[int]]:
    """Collapse the full crossing-number cluster before tracing outgoing arms."""
    xs = np.array([p[0] for p in cluster], dtype=float)
    ys = np.array([p[1] for p in cluster], dtype=float)
    cx_f, cy_f = float(np.mean(xs)), float(np.mean(ys))
    # Pick the cluster pixel closest to the centroid as the reported location.
    x, y, _ = min(cluster, key=lambda p: (p[0] - cx_f) ** 2 + (p[1] - cy_f) ** 2)
    h, w = skeleton_bool.shape
    pad = max_steps + 6
    x0, x1 = max(0, int(xs.min()) - pad), min(w, int(xs.max()) + pad + 1)
    y0, y1 = max(0, int(ys.min()) - pad), min(h, int(ys.max()) + pad + 1)
    local = skeleton_bool[y0:y1, x0:x1].copy()
    zone = np.zeros_like(local, dtype=np.uint8)
    for px, py, _ in cluster:
        cv2.circle(zone, (px - x0, py - y0), 2, 1, thickness=-1)
    zone = (zone > 0) & local
    remainder = local & ~zone
    n, labels = cv2.connectedComponents(remainder.astype(np.uint8), 8)
    zone_dilated = cv2.dilate(zone.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
    desc: list[tuple[float, int]] = []
    oy, ox = y - y0, x - x0
    for label in range(1, n):
        comp = labels == label
        boundary = comp & zone_dilated
        if not np.any(boundary):
            continue
        by, bx = np.nonzero(boundary)
        k = int(np.argmin((by - oy) ** 2 + (bx - ox) ** 2))
        start = (int(by[k]), int(bx[k]))
        queue = [start]
        dist = {start: 0}
        qpos = 0
        while qpos < len(queue):
            cy, cx = queue[qpos]
            qpos += 1
            if dist[(cy, cx)] >= max_steps:
                continue
            for dy, dx in _OFFSETS_8:
                ny, nx = cy + dy, cx + dx
                if 0 <= ny < comp.shape[0] and 0 <= nx < comp.shape[1] and comp[ny, nx]:
                    if (ny, nx) not in dist:
                        dist[(ny, nx)] = dist[(cy, cx)] + 1
                        queue.append((ny, nx))
        (ey, ex), length = max(dist.items(), key=lambda item: item[1])
        angle = math.atan2((ey + y0) - y, (ex + x0) - x) % (2 * math.pi)
        desc.append((angle, int(length)))
    # Deduplicate arms only when they are nearly identical.
    desc.sort(key=lambda z: z[1], reverse=True)
    unique: list[tuple[float, int]] = []
    for angle, length in desc:
        if all(_circular_angle_error(angle, other) >= math.radians(18) for other, _ in unique):
            unique.append((angle, length))
    unique.sort(key=lambda z: z[0])
    return int(x), int(y), [a for a, _ in unique], [l for _, l in unique]


def _candidate_pixels(skeleton_bool: np.ndarray) -> list[tuple[int, int, str]]:
    out = []
    ys, xs = np.nonzero(skeleton_bool)
    for y, x in zip(ys.tolist(), xs.tolist()):
        cn = crossing_number_at(skeleton_bool, y, x)
        if cn == 1:
            out.append((x, y, "ending"))
        elif cn == 3:
            out.append((x, y, "bifurcation"))
    return out


def _cluster_candidates(candidates: list[tuple[int, int, str]], radius: float = 4.5) -> list[list[tuple[int, int, str]]]:
    clusters: list[list[tuple[int, int, str]]] = []
    unused = set(range(len(candidates)))
    while unused:
        seed = unused.pop()
        cluster_idx = {seed}
        frontier = [seed]
        while frontier:
            i = frontier.pop()
            xi, yi, ti = candidates[i]
            near = []
            for j in list(unused):
                xj, yj, tj = candidates[j]
                if tj == ti and math.hypot(xi - xj, yi - yj) <= radius:
                    near.append(j)
            for j in near:
                unused.remove(j)
                cluster_idx.add(j)
                frontier.append(j)
        clusters.append([candidates[i] for i in cluster_idx])
    return clusters


def _circular_angle_error(a: float, b: float) -> float:
    return abs((a - b + math.pi) % (2 * math.pi) - math.pi)


def suppress_broken_ridge_endpoint_pairs(
    minutiae: list[Minutia],
    max_gap: float,
    tolerance_deg: float,
) -> tuple[list[Minutia], int]:
    """Suppress paired endings that face one another across a short gap.

    The skeleton contains occasional breaks in otherwise continuous ridges.
    Crossing number turns each side of such a break into a false ending.  The
    pair test uses the skeleton-derived branch direction, not the block field.
    """
    remove: set[int] = set()
    tolerance = math.radians(tolerance_deg)
    endings = [(i, m) for i, m in enumerate(minutiae) if m.kind == "ending" and m.branch_angles_rad]
    for pos, (i, first) in enumerate(endings):
        if i in remove:
            continue
        for j, second in endings[pos + 1:]:
            if j in remove:
                continue
            dx, dy = second.x - first.x, second.y - first.y
            gap = math.hypot(dx, dy)
            if not (3.0 <= gap <= max_gap):
                continue
            direction_12 = math.atan2(dy, dx) % (2 * math.pi)
            missing_1 = (first.branch_angles_rad[0] + math.pi) % (2 * math.pi)
            missing_2 = (second.branch_angles_rad[0] + math.pi) % (2 * math.pi)
            if (
                _circular_angle_error(direction_12, missing_1) < tolerance
                and _circular_angle_error((direction_12 + math.pi) % (2 * math.pi), missing_2) < tolerance
            ):
                remove.add(i)
                remove.add(j)
                break
    return [m for i, m in enumerate(minutiae) if i not in remove], len(remove)


def extract_and_filter_minutiae(
    skeleton: np.ndarray,
    inner_roi_mask: np.ndarray,
    coherence_dense: np.ndarray,
    cfg: PipelineConfig,
) -> tuple[list[Minutia], dict]:
    sk = skeleton > 0
    raw = _candidate_pixels(sk)
    clusters = _cluster_candidates(raw)
    roi_dist = cv2.distanceTransform((inner_roi_mask > 0).astype(np.uint8), cv2.DIST_L2, 5)
    accepted: list[Minutia] = []
    rejection = {
        "outside_inner_roi": 0,
        "low_coherence": 0,
        "invalid_branch_count": 0,
        "short_branch": 0,
        "near_duplicate": 0,
    }
    for cluster in clusters:
        kind = cluster[0][2]
        x, y, angles, lengths = branch_descriptors_for_candidate_cluster(
            sk, cluster, cfg.branch_trace_length
        )
        if inner_roi_mask[y, x] == 0 or roi_dist[y, x] < cfg.minutia_roi_distance:
            rejection["outside_inner_roi"] += 1
            continue
        coh = float(coherence_dense[y, x])
        if coh < cfg.minutia_min_coherence:
            rejection["low_coherence"] += 1
            continue
        expected = 1 if kind == "ending" else 3
        if len(angles) != expected:
            rejection["invalid_branch_count"] += 1
            continue
        if min(lengths) < cfg.branch_min_length:
            rejection["short_branch"] += 1
            continue
        if any(math.hypot(x - m.x, y - m.y) < cfg.minutia_min_separation for m in accepted):
            rejection["near_duplicate"] += 1
            continue
        q = float(np.clip(0.55 * coh + 0.45 * min(1.0, min(lengths) / cfg.branch_trace_length), 0.0, 1.0))
        accepted.append(
            Minutia(
                x=int(x), y=int(y), kind=kind,
                branch_angles_rad=[float(a) for a in angles],
                branch_lengths_px=[int(v) for v in lengths],
                coherence=coh,
                roi_distance_px=float(roi_dist[y, x]),
                quality_proxy=q,
            )
        )
    accepted_before_pair_suppression = len(accepted)
    accepted, suppressed_pair_endings = suppress_broken_ridge_endpoint_pairs(
        accepted,
        max_gap=cfg.broken_ridge_pair_max_gap,
        tolerance_deg=cfg.broken_ridge_pair_angle_tolerance_deg,
    )
    diag = {
        "raw_crossing_number_candidates": len(raw),
        "candidate_clusters": len(clusters),
        "accepted_before_broken_ridge_pair_suppression": accepted_before_pair_suppression,
        "suppressed_broken_ridge_endings": suppressed_pair_endings,
        "accepted_minutiae": len(accepted),
        "accepted_endings": sum(m.kind == "ending" for m in accepted),
        "accepted_bifurcations": sum(m.kind == "bifurcation" for m in accepted),
        "rejections": rejection,
    }
    return accepted, diag


def wrap_axial_difference(a: float, b: float) -> float:
    d = a - b
    while d > math.pi / 2:
        d -= math.pi
    while d < -math.pi / 2:
        d += math.pi
    return d


def poincare_index_block(theta: np.ndarray, br: int, bc: int) -> float:
    ring = [
        (br - 1, bc - 1), (br - 1, bc), (br - 1, bc + 1), (br, bc + 1),
        (br + 1, bc + 1), (br + 1, bc), (br + 1, bc - 1), (br, bc - 1),
    ]
    vals = [float(theta[r, c]) for r, c in ring]
    return sum(wrap_axial_difference(vals[(i + 1) % 8], vals[i]) for i in range(8))


def poincare_index_dense(theta: np.ndarray, x: int, y: int, radius: int, samples: int = 24) -> float:
    h, w = theta.shape
    vals = []
    for k in range(samples):
        phi = 2.0 * math.pi * k / samples
        xx = int(round(x + radius * math.cos(phi)))
        yy = int(round(y + radius * math.sin(phi)))
        if not (0 <= xx < w and 0 <= yy < h):
            return 0.0
        vals.append(float(theta[yy, xx]))
    return sum(wrap_axial_difference(vals[(i + 1) % samples], vals[i]) for i in range(samples))


def _cluster_points(points: list[tuple[int, int, float, str]], radius: float) -> list[list[tuple[int, int, float, str]]]:
    clusters = []
    unused = set(range(len(points)))
    while unused:
        seed = unused.pop()
        members = {seed}
        frontier = [seed]
        while frontier:
            i = frontier.pop()
            xi, yi, _, ti = points[i]
            for j in list(unused):
                xj, yj, _, tj = points[j]
                if tj == ti and math.hypot(xi - xj, yi - yj) <= radius:
                    unused.remove(j)
                    members.add(j)
                    frontier.append(j)
        clusters.append([points[i] for i in members])
    return clusters


def estimate_singularity_branches(
    theta_dense: np.ndarray,
    x: int,
    y: int,
    kind: str,
    radius: int,
) -> list[float]:
    # Experimental visualization: on a ring around the singularity, score where
    # the local ridge tangent is radial.  Delta points ideally produce three
    # well-separated arms.  Core direction is not unique; we retain the two
    # strongest separated directions as a local topology cue only.
    samples = 180
    phis = np.linspace(0, 2 * np.pi, samples, endpoint=False)
    scores = np.zeros(samples, dtype=float)
    h, w = theta_dense.shape
    for i, phi in enumerate(phis):
        xx = int(round(x + radius * math.cos(phi)))
        yy = int(round(y + radius * math.sin(phi)))
        if 0 <= xx < w and 0 <= yy < h:
            theta = float(theta_dense[yy, xx])
            # Axial alignment: 1 when tangent is parallel to radial line.
            scores[i] = abs(math.cos(theta - phi))
    # Circular smoothing and peak extraction.
    padded = np.r_[scores, scores, scores]
    sm = ndimage.gaussian_filter1d(padded, sigma=3.0, mode="wrap")[samples:2 * samples]
    peaks, _ = find_peaks(sm, distance=25, prominence=0.01)
    if len(peaks) == 0:
        peaks = np.argsort(sm)[-6:]
    ranked = sorted(peaks.tolist(), key=lambda i: sm[i], reverse=True)
    needed = 3 if kind == "delta" else 2
    chosen: list[float] = []
    min_sep = math.radians(55 if kind == "delta" else 80)
    for idx in ranked:
        a = float(phis[idx] % (2 * math.pi))
        if all(min(abs(a - b), 2 * math.pi - abs(a - b)) >= min_sep for b in chosen):
            chosen.append(a)
        if len(chosen) == needed:
            break
    return sorted(chosen)


def detect_singular_points(
    orientation_blocks: np.ndarray,
    coherence_blocks: np.ndarray,
    orientation_dense: np.ndarray,
    coherence_dense: np.ndarray,
    inner_roi_mask: np.ndarray,
    cfg: PipelineConfig,
) -> tuple[list[SingularPoint], dict]:
    bs = cfg.block_size
    bh, bw = orientation_blocks.shape
    block_candidates: list[tuple[int, int, float, str]] = []
    for br in range(1, bh - 1):
        for bc in range(1, bw - 1):
            x = bc * bs + bs // 2
            y = br * bs + bs // 2
            if y >= inner_roi_mask.shape[0] or x >= inner_roi_mask.shape[1]:
                continue
            if inner_roi_mask[y, x] == 0:
                continue
            if coherence_blocks[br, bc] < cfg.singular_min_coherence:
                continue
            pi = poincare_index_block(orientation_blocks, br, bc)
            if pi > math.pi / 2:
                block_candidates.append((x, y, pi, "core"))
            elif pi < -math.pi / 2:
                block_candidates.append((x, y, pi, "delta"))
    clusters = _cluster_points(block_candidates, radius=1.6 * bs)
    points: list[SingularPoint] = []
    for cluster in clusters:
        kind = cluster[0][3]
        # Start from the strongest block candidate, then search pixel positions
        # around it using a dense Poincare ring.
        cluster.sort(key=lambda p: abs(p[2]), reverse=True)
        cx, cy, _, _ = cluster[0]
        best = None
        for yy in range(max(1, cy - cfg.singular_search_radius_px), min(inner_roi_mask.shape[0] - 1, cy + cfg.singular_search_radius_px + 1)):
            for xx in range(max(1, cx - cfg.singular_search_radius_px), min(inner_roi_mask.shape[1] - 1, cx + cfg.singular_search_radius_px + 1)):
                if inner_roi_mask[yy, xx] == 0:
                    continue
                val = poincare_index_dense(
                    orientation_dense,
                    xx,
                    yy,
                    radius=cfg.singular_ring_radius_px,
                )
                if kind == "core" and val <= 0:
                    continue
                if kind == "delta" and val >= 0:
                    continue
                score = abs(val) * (0.5 + 0.5 * float(coherence_dense[yy, xx]))
                if best is None or score > best[0]:
                    best = (score, xx, yy, val)
        if best is None:
            xx, yy, val = cx, cy, cluster[0][2]
        else:
            _, xx, yy, val = best
        # Suppress duplicate refined singularities.
        if any(p.kind == kind and math.hypot(p.x - xx, p.y - yy) < bs for p in points):
            continue
        branches = estimate_singularity_branches(
            orientation_dense, xx, yy, kind, radius=max(12, 2 * cfg.singular_ring_radius_px)
        )
        points.append(
            SingularPoint(
                x=int(xx), y=int(yy), kind=kind,
                poincare_index_rad=float(val),
                branch_angles_rad=[float(a) for a in branches],
                coherence=float(coherence_dense[yy, xx]),
            )
        )
    points.sort(key=lambda p: (p.kind, p.y, p.x))
    diag = {
        "block_singularity_candidates": len(block_candidates),
        "singularity_clusters": len(clusters),
        "detected_cores": sum(p.kind == "core" for p in points),
        "detected_deltas": sum(p.kind == "delta" for p in points),
    }
    return points, diag


def run_pipeline(image_path: str | Path, cfg: PipelineConfig | None = None) -> PipelineResult:
    cfg = cfg or PipelineConfig()
    original = load_grayscale(image_path)
    normalized, enhanced = normalize_and_clahe(original, cfg)
    roi, inner = compute_roi_mask(enhanced, cfg)
    theta_b, coh_b, theta_dense, coh_dense = estimate_orientation_fields(enhanced, roi, cfg)
    binary, skeleton, sk_diag = binarize_and_skeletonize(original, roi, cfg)
    minutiae, min_diag = extract_and_filter_minutiae(skeleton, inner, coh_dense, cfg)
    singular, sing_diag = detect_singular_points(theta_b, coh_b, theta_dense, coh_dense, inner, cfg)
    diagnostics = {
        "image_shape": list(original.shape),
        "configuration": asdict(cfg),
        **sk_diag,
        **min_diag,
        **sing_diag,
    }
    return PipelineResult(
        original=original,
        normalized=normalized,
        enhanced=enhanced,
        roi_mask=roi,
        inner_roi_mask=inner,
        orientation_blocks=theta_b,
        coherence_blocks=coh_b,
        orientation_dense=theta_dense,
        coherence_dense=coh_dense,
        binary_ridges=binary,
        skeleton=skeleton,
        minutiae=minutiae,
        singular_points=singular,
        diagnostics=diagnostics,
    )


def ensure_bgr(img: np.ndarray) -> np.ndarray:
    if img.ndim == 2:
        return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    return img.copy()


def draw_orientation_field(
    background: np.ndarray,
    theta_blocks: np.ndarray,
    coherence_blocks: np.ndarray,
    cfg: PipelineConfig,
    coherence_threshold: float = 0.15,
    line_length: int = 6,
) -> np.ndarray:
    vis = ensure_bgr(background)
    h, w = background.shape[:2]
    bs = cfg.block_size
    for br in range(theta_blocks.shape[0]):
        for bc in range(theta_blocks.shape[1]):
            if coherence_blocks[br, bc] < coherence_threshold:
                continue
            cx, cy = bc * bs + bs // 2, br * bs + bs // 2
            if not (0 <= cx < w and 0 <= cy < h):
                continue
            a = float(theta_blocks[br, bc])
            dx, dy = int(line_length * math.cos(a)), int(line_length * math.sin(a))
            cv2.line(vis, (cx - dx, cy - dy), (cx + dx, cy + dy), (0, 180, 0), 1, cv2.LINE_AA)
    return vis


def draw_minutiae_branches(background: np.ndarray, minutiae: Sequence[Minutia], line_length: int = 15) -> np.ndarray:
    vis = ensure_bgr(background)
    for m in minutiae:
        color = (0, 0, 230) if m.kind == "ending" else (230, 0, 0)
        cv2.circle(vis, (m.x, m.y), 3, color, -1, cv2.LINE_AA)
        for angle in m.branch_angles_rad:
            dx = int(line_length * math.cos(angle))
            dy = int(line_length * math.sin(angle))
            cv2.line(vis, (m.x, m.y), (m.x + dx, m.y + dy), (0, 190, 0), 1, cv2.LINE_AA)
    return vis


def draw_singular_points(background: np.ndarray, points: Sequence[SingularPoint], line_length: int = 24) -> np.ndarray:
    vis = ensure_bgr(background)
    for p in points:
        color = (0, 190, 230) if p.kind == "core" else (230, 210, 0)
        label = "C" if p.kind == "core" else "D"
        cv2.circle(vis, (p.x, p.y), 8, color, 2, cv2.LINE_AA)
        cv2.putText(vis, label, (p.x + 7, p.y - 7), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)
        for angle in p.branch_angles_rad:
            dx = int(line_length * math.cos(angle))
            dy = int(line_length * math.sin(angle))
            cv2.line(vis, (p.x, p.y), (p.x + dx, p.y + dy), color, 2, cv2.LINE_AA)
    return vis


def save_result_tables(result: PipelineResult, out_dir: str | Path) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with (out / "minutiae.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "x", "y", "kind", "orientation_rad", "branch_angles_rad",
                "branch_lengths_px", "coherence", "roi_distance_px", "quality_proxy",
            ],
        )
        writer.writeheader()
        for m in result.minutiae:
            writer.writerow({
                "x": m.x,
                "y": m.y,
                "kind": m.kind,
                "orientation_rad": "" if m.orientation_rad is None else f"{m.orientation_rad:.8f}",
                "branch_angles_rad": json.dumps([round(v, 8) for v in m.branch_angles_rad]),
                "branch_lengths_px": json.dumps(m.branch_lengths_px),
                "coherence": f"{m.coherence:.8f}",
                "roi_distance_px": f"{m.roi_distance_px:.4f}",
                "quality_proxy": f"{m.quality_proxy:.8f}",
            })
    with (out / "singular_points.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["x", "y", "kind", "poincare_index_rad", "branch_angles_rad", "coherence"],
        )
        writer.writeheader()
        for p in result.singular_points:
            writer.writerow({
                "x": p.x,
                "y": p.y,
                "kind": p.kind,
                "poincare_index_rad": f"{p.poincare_index_rad:.8f}",
                "branch_angles_rad": json.dumps([round(v, 8) for v in p.branch_angles_rad]),
                "coherence": f"{p.coherence:.8f}",
            })
    (out / "diagnostics.json").write_text(json.dumps(result.diagnostics, indent=2), encoding="utf-8")
