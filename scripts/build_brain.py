#!/usr/bin/env python3
"""
Grows the cerebrum's folds (gyri) once and saves them to scripts/brain_folds.json.

Real cortex folds look like a Turing pattern, so this runs a Gray-Scott
reaction-diffusion simulation inside the cerebrum outline from generate.py and
traces the ridges with marching squares. It only needs re-running if the
outline changes; generate.py just reads the JSON, so the scheduled Action stays
standard-library only.

    pip install numpy
    python scripts/build_brain.py [--top] [--preview out.png]   # --top: top-view hemisphere
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("generate", HERE / "generate.py")
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)

MODES = {  # outline, box size in hero coordinates, grooves to carve, output file
    "side": (lambda: gen.CEREBRUM, 318, 280, lambda: [p for p, _w in gen.BRAIN_SULCI[:2]], "brain_folds.json"),
    "top": (lambda: gen.TOP_LEFT, 212, 270, lambda: gen.TOP_GROOVES, "brain_top_folds.json"),
}
CELL = 0.72              # simulation cell size in those units (smaller = finer folds)
F, K = 0.037, 0.06       # Gray-Scott feed/kill in the labyrinth regime
DU, DV = 1.0, 0.5
STEPS = 9000


def inside_mask(poly: list[tuple[float, float]], xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """Vectorised even-odd point-in-polygon test."""
    inside = np.zeros(xs.shape, dtype=bool)
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        if y1 == y2:
            continue
        cross = (y1 > ys) != (y2 > ys)
        xint = x1 + (ys - y1) * (x2 - x1) / (y2 - y1)
        inside ^= cross & (xs < xint)
    return inside


def laplacian(a: np.ndarray) -> np.ndarray:
    return (0.2 * (np.roll(a, 1, 0) + np.roll(a, -1, 0) + np.roll(a, 1, 1) + np.roll(a, -1, 1))
            + 0.05 * (np.roll(np.roll(a, 1, 0), 1, 1) + np.roll(np.roll(a, 1, 0), -1, 1)
                      + np.roll(np.roll(a, -1, 0), 1, 1) + np.roll(np.roll(a, -1, 0), -1, 1)) - a)


def march(field: np.ndarray, level: float) -> list[list[tuple[float, float]]]:
    """Marching squares → joined polylines in grid coordinates (x=col, y=row)."""
    rows, cols = field.shape
    segs = []

    def interp(p1, p2, v1, v2):
        t = (level - v1) / (v2 - v1) if v2 != v1 else 0.5
        return (p1[0] + t * (p2[0] - p1[0]), p1[1] + t * (p2[1] - p1[1]))

    for r in range(rows - 1):
        for c in range(cols - 1):
            v = (field[r, c], field[r, c + 1], field[r + 1, c + 1], field[r + 1, c])
            idx = sum(1 << i for i, x in enumerate(v) if x > level)
            if idx in (0, 15):
                continue
            corners = [(c, r), (c + 1, r), (c + 1, r + 1), (c, r + 1)]
            edge = {e: interp(corners[e], corners[(e + 1) % 4], v[e], v[(e + 1) % 4]) for e in range(4)}
            table = {1: [(3, 0)], 2: [(0, 1)], 3: [(3, 1)], 4: [(1, 2)], 5: [(3, 0), (1, 2)], 6: [(0, 2)],
                     7: [(3, 2)], 8: [(2, 3)], 9: [(0, 2)], 10: [(0, 1), (2, 3)], 11: [(1, 2)], 12: [(1, 3)],
                     13: [(0, 1)], 14: [(3, 0)]}
            for a, b in table[idx]:
                segs.append((edge[a], edge[b]))

    key = lambda p: (round(p[0], 3), round(p[1], 3))  # noqa: E731
    ends: dict = {}
    for i, (a, b) in enumerate(segs):
        ends.setdefault(key(a), []).append(i)
        ends.setdefault(key(b), []).append(i)
    used = [False] * len(segs)
    lines = []
    for i in range(len(segs)):
        if used[i]:
            continue
        used[i] = True
        line = [segs[i][0], segs[i][1]]
        for grow_front in (False, True):
            while True:
                tip = line[0] if grow_front else line[-1]
                nxt = next((j for j in ends.get(key(tip), []) if not used[j]), None)
                if nxt is None:
                    break
                used[nxt] = True
                a, b = segs[nxt]
                other = b if key(a) == key(tip) else a
                line.insert(0, other) if grow_front else line.append(other)
        lines.append(line)
    return lines


def rdp(pts, eps):
    if len(pts) < 3:
        return pts
    a, b = np.array(pts[0]), np.array(pts[-1])
    ab = b - a
    n = np.hypot(*ab) or 1e-9
    d = [abs(ab[0] * (p[1] - a[1]) - ab[1] * (p[0] - a[0])) / n for p in pts[1:-1]]
    i = int(np.argmax(d)) + 1
    if d[i - 1] > eps:
        return rdp(pts[: i + 1], eps)[:-1] + rdp(pts[i:], eps)
    return [pts[0], pts[-1]]


def main() -> None:
    global CELL
    mode = "top" if "--top" in sys.argv else "side"
    if mode == "top":
        CELL = 0.52          # finer folds: the top view is drawn as dense anatomical line-art
    outline_fn, W, H, grooves_fn, out_name = MODES[mode]
    _, poly = gen._catmull([(u * W, v * H) for u, v in outline_fn()], closed=True)
    x0, y0 = min(p[0] for p in poly) - 4, min(p[1] for p in poly) - 4
    x1, y1 = max(p[0] for p in poly) + 4, max(p[1] for p in poly) + 4
    cols, rows = int((x1 - x0) / CELL) + 1, int((y1 - y0) / CELL) + 1
    gx, gy = np.meshgrid(x0 + np.arange(cols) * CELL, y0 + np.arange(rows) * CELL)
    mask = inside_mask(poly, gx, gy)

    # Grow the pattern over the whole box, then cut it to the outline, so folds
    # meet the edge the way a real cortex does instead of running parallel to it.
    rng = np.random.default_rng(11)
    U = np.ones((rows, cols))
    V = np.zeros((rows, cols))
    for _ in range(rows * cols // 120):  # seed patches; single cells die out in this regime
        r, c = rng.integers(0, rows - 4), rng.integers(0, cols - 4)
        V[r:r + 4, c:c + 4], U[r:r + 4, c:c + 4] = 1.0, 0.5
    # Carve the deep grooves; folds then grow alongside them.
    groove = np.zeros((rows, cols), dtype=bool)
    for pts in grooves_fn():
        _, dense = gen._catmull([(u * W, v * H) for u, v in pts], closed=False)
        for x, y in dense:
            r0, c0 = int(round((y - y0) / CELL)), int(round((x - x0) / CELL))
            groove[max(0, r0 - 2):r0 + 3, max(0, c0 - 2):c0 + 3] = True
    for _ in range(STEPS):
        uvv = U * V * V
        U += DU * laplacian(U) - uvv + F * (1 - U)
        V += DV * laplacian(V) + uvv - (F + K) * V
        U[groove], V[groove] = 1.0, 0.0

    from scipy.ndimage import binary_erosion
    keep = binary_erosion(mask, iterations=3) & ~groove
    lines = []
    for line in march(V, 0.22):
        run = []
        for x, y in line + [(-1, -1)]:
            r, c = int(round(y)), int(round(x))
            if 0 <= r < rows and 0 <= c < cols and keep[r, c]:
                run.append((x0 + x * CELL, y0 + y * CELL))
                continue
            if len(run) > 2:
                pts = rdp(run, 0.35)
                length = sum(np.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]) for i in range(len(pts) - 1))
                if length > 6:
                    lines.append([[round(px / W, 4), round(py / H, 4)] for px, py in pts])
            run = []
    out = HERE / out_name
    out.write_text(json.dumps({"source": "Gray-Scott F=%s K=%s steps=%d" % (F, K, STEPS), "folds": lines},
                              separators=(",", ":")), encoding="utf-8")
    print(f"{mode}: {len(lines)} folds, {sum(len(l) for l in lines)} points -> {out.name}")

    if "--preview" in sys.argv:
        from PIL import Image, ImageDraw
        S = 3
        im = Image.new("RGB", (W * S, H * S), (16, 16, 18))
        dr = ImageDraw.Draw(im)
        dr.line([(x * S, y * S) for x, y in poly + poly[:1]], fill=(240, 240, 245), width=3)
        for l in lines:
            dr.line([(u * W * S, v * H * S) for u, v in l], fill=(200, 200, 205), width=2)
        im.save(sys.argv[sys.argv.index("--preview") + 1])


if __name__ == "__main__":
    main()
