"""Gameplay filmed off a TV or monitor with a phone: find the screen, straighten it, crop the room away.

A phone pointed at a TV gives a tilted, wobbling picture of the screen in the middle of a dark room.
Used as-is, the game fills a third of the reel. This module:
  1. looks at the clip twice a second and finds the screen in each frame: the bright, four-sided
     shape with straight edges (the TV glows, the room around it is dark)
  2. decides whether the clip really was filmed off a screen (the shape is found in most frames)
  3. for every part of the reel, tracks the four corners more closely (six times a second), smooths
     them, and builds an ffmpeg `perspective` filter that pins the corners to a flat 16:9 frame.
     Hand shake disappears with it: the screen stays still while the phone moved.
Moments where the camera pointed away from the screen, or the screen was black (loading, death
screens), are marked so the moment finder avoids them.

Only numpy is used, so it runs on GitHub's computers and on an iPhone alike.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import media

ANALYSIS_FPS = 2.0         # matches highlights.WIN (0.5 s)
FINE_FPS = 6.0             # corner tracking for the parts that go into a reel
KEY_FPS = 3.0              # keyframes written into the filter expression
INSET = 0.012              # pull the corners in a little so the TV's bezel never shows


@dataclass
class Track:
    filmed: bool
    times: np.ndarray                      # sample times (seconds into the clip)
    quads: np.ndarray                      # (n, 4, 2) corners TL, TR, BL, BR in source pixels (NaN = not found)
    found: np.ndarray                      # (n,) bool
    dark: np.ndarray                       # (n,) bool: the screen is (almost) black there
    out_w: int = 1920
    out_h: int = 1080
    src_w: int = 0
    src_h: int = 0
    share: float = 0.0                     # share of samples where the screen was found
    notes: list = field(default_factory=list)
    rect: np.ndarray | None = None         # (n, 36, 64, 3) the screen alone, straightened, per sample
    shake: np.ndarray | None = None        # (n,) how far the phone moved since the last sample (share of width)

    def median_quad(self, t0: float | None = None, t1: float | None = None) -> np.ndarray | None:
        sel = self.found.copy()
        if t0 is not None:
            sel &= (self.times >= t0) & (self.times <= t1)
        if not sel.any():
            sel = self.found
        if not sel.any():
            return None
        return np.median(self.quads[sel], axis=0)

    def penalty(self, n: int, step: float) -> np.ndarray:
        """Per analysis step: 1 where the screen is missing or black, else 0 (for the moment finder)."""
        out = np.zeros(n, dtype=np.float32)
        if not self.filmed:
            return out
        for i in range(n):
            t = i * step + step / 2
            j = int(np.argmin(np.abs(self.times - t))) if len(self.times) else -1
            if j >= 0 and (not self.found[j] or self.dark[j]):
                out[i] = 1.0
        return out


# ── finding the screen in one small grey frame ──────────────────────────────

def _otsu(g: np.ndarray) -> float:
    hist = np.bincount(g.ravel().astype(np.uint8), minlength=256).astype(np.float64)
    p = hist / max(hist.sum(), 1)
    omega = np.cumsum(p)
    mu = np.cumsum(p * np.arange(256))
    mu_t = mu[-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        sb = (mu_t * omega - mu) ** 2 / (omega * (1 - omega))
    sb = np.nan_to_num(sb)
    return float(np.argmax(sb))


def _largest_component(mask: np.ndarray) -> np.ndarray | None:
    """Rows of the largest 4-connected component as (row, x0, x1) runs, holes filled per row."""
    h, w = mask.shape
    padded = np.zeros((h, w + 2), dtype=np.int8)
    padded[:, 1:-1] = mask
    d = np.diff(padded, axis=1)
    rows_s, cols_s = np.nonzero(d == 1)
    rows_e, cols_e = np.nonzero(d == -1)
    if len(rows_s) == 0:
        return None
    runs = np.stack([rows_s, cols_s, cols_e], axis=1)          # [row, start, end) sorted by row then col
    parent = list(range(len(runs)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    row_index: dict[int, list[int]] = {}
    for i, r in enumerate(runs[:, 0]):
        row_index.setdefault(int(r), []).append(i)
    for r, ids in row_index.items():
        prev = row_index.get(r - 1)
        if not prev:
            continue
        j = 0
        for i in ids:
            s, e = runs[i, 1], runs[i, 2]
            while j < len(prev) and runs[prev[j], 2] <= s:
                j += 1
            k = j
            while k < len(prev) and runs[prev[k], 1] < e:
                a, b = find(i), find(prev[k])
                if a != b:
                    parent[a] = b
                k += 1
    roots = np.array([find(i) for i in range(len(runs))])
    sizes = np.bincount(roots, weights=(runs[:, 2] - runs[:, 1]).astype(np.float64))
    best = int(np.argmax(sizes))
    comp = runs[roots == best]
    # one span per row: from the leftmost start to the rightmost end (fills holes inside the screen)
    out = {}
    for r, s, e in comp:
        r = int(r)
        if r in out:
            out[r] = (min(out[r][0], s), max(out[r][1], e))
        else:
            out[r] = (s, e)
    return np.array([(r, s, e) for r, (s, e) in sorted(out.items())], dtype=np.float64)


def _ransac_line(pts: np.ndarray, tol: float, rng: np.random.Generator) -> tuple[np.ndarray, float, float] | None:
    """Line through the most points (RANSAC, then a least-squares refit). Returns (a, b, c), rms, inlier share."""
    n = len(pts)
    if n < 8:
        return None
    best, best_in = None, None
    for _ in range(60):
        i, j = rng.choice(n, 2, replace=False)
        d = pts[j] - pts[i]
        norm = np.hypot(*d)
        if norm < 3:
            continue
        nrm = np.array([-d[1], d[0]]) / norm
        line = np.array([nrm[0], nrm[1], -nrm @ pts[i]])
        inl = np.abs(pts @ line[:2] + line[2]) < tol
        if best_in is None or inl.sum() > best_in.sum():
            best, best_in = line, inl
    if best is None or best_in.sum() < 8:
        return None
    sel = pts[best_in]
    for _ in range(2):                                # refine on the inliers
        c = sel.mean(axis=0)
        _, _, vt = np.linalg.svd(sel - c, full_matrices=False)
        nrm = vt[1]
        best = np.array([nrm[0], nrm[1], -nrm @ c])
        res = np.abs(pts @ best[:2] + best[2])
        inl = res < tol
        if inl.sum() >= 8:
            sel = pts[inl]
    res = np.abs(sel @ best[:2] + best[2])
    return best, float(np.sqrt(np.mean(res ** 2))), float(len(sel) / n)


def _intersect(l1: np.ndarray, l2: np.ndarray) -> np.ndarray | None:
    a = np.array([[l1[0], l1[1]], [l2[0], l2[1]]])
    if abs(np.linalg.det(a)) < 1e-9:
        return None
    return np.linalg.solve(a, -np.array([l1[2], l2[2]]))


def _quad_area(q: np.ndarray) -> float:
    tl, tr, bl, br = q
    poly = np.array([tl, tr, br, bl])
    x, y = poly[:, 0], poly[:, 1]
    return 0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def _inside(q: np.ndarray, h: int, w: int) -> np.ndarray:
    tl, tr, bl, br = q
    poly = [tl, tr, br, bl]
    yy, xx = np.mgrid[0:h, 0:w]
    inside = np.ones((h, w), dtype=bool)
    sign = None
    for i in range(4):
        a, b = poly[i], poly[(i + 1) % 4]
        cross = (b[0] - a[0]) * (yy - a[1]) - (b[1] - a[1]) * (xx - a[0])
        if sign is None:
            sign = np.sign(np.sum(cross[h // 2, w // 2]) or 1)
        inside &= cross * sign >= 0
    return inside


_last = None


def find_quad(g: np.ndarray) -> tuple[np.ndarray | None, dict]:
    """Corners (TL, TR, BL, BR) of a screen in one grey frame, in that frame's pixels, or None.

    The bright blob (Otsu threshold) only gives a rough window, because the TV's glow on the wall joins
    it. The edges themselves are found as the sharpest brightness step along every row and column of
    that window (the glow fades gently, the screen's edge is a hard step), then a straight line is fitted
    through each side's edge points, ignoring stray points."""
    h, w = g.shape
    info = {}
    gf = g.astype(np.float32)
    k = np.ones(3, dtype=np.float32) / 3                       # light blur against camera noise
    gf = np.apply_along_axis(lambda r: np.convolve(r, k, mode="same"), 1, gf)
    gf = np.apply_along_axis(lambda c: np.convolve(c, k, mode="same"), 0, gf)
    if np.percentile(gf, 95) - np.percentile(gf, 5) < 35:      # flat frame: nothing to find
        return None, {"why": "flat"}
    comp = _largest_component(gf > _otsu(gf))
    if comp is None or len(comp) < h * 0.12:
        return None, {"why": "small"}
    rows = comp[:, 0]
    y0, y1 = rows.min(), rows.max()
    x0, x1 = np.percentile(comp[:, 1], 5), np.percentile(comp[:, 2], 95)
    cy, cx = (y0 + y1) / 2, (x0 + x1) / 2
    my = 0.08 * h
    gy = np.zeros_like(gf); gy[2:-2] = gf[4:] - gf[:-4]          # brightness step down the column
    gx = np.zeros_like(gf); gx[:, 2:-2] = gf[:, 4:] - gf[:, :-4]

    def col_edge(lo, hi, sign):
        xs = np.arange(2, w - 2)                 # every column, so the lines can be followed to their ends
        lo, hi = int(max(2, lo)), int(min(h - 2, hi))
        if hi - lo < 4 or not len(xs):
            return np.zeros((0, 2))
        band = gy[lo:hi, xs] * sign
        ys = band.argmax(axis=0)
        strong = band[ys, np.arange(len(xs))] > 14
        return np.stack([xs[strong], ys[strong] + lo], axis=1).astype(np.float64)

    def row_edge(lo, hi, sign, ya, yb):
        ys = np.arange(int(max(2, ya)), int(min(h - 2, yb)))
        lo, hi = int(max(2, lo)), int(min(w - 2, hi))
        if hi - lo < 4 or not len(ys):
            return np.zeros((0, 2))
        band = gx[ys, lo:hi] * sign
        xs = band.argmax(axis=1)
        strong = band[np.arange(len(ys)), xs] > 14
        return np.stack([xs[strong] + lo, ys[strong]], axis=1).astype(np.float64)

    top = col_edge(y0 - my, cy, +1)           # dark (or glow) above, bright screen below
    bottom = col_edge(cy, y1 + my, -1)        # bright screen above, dark below
    rng = np.random.default_rng(7)
    tol = max(1.2, 0.006 * w)
    lines, rms, share = {}, [], []
    for name, pts in (("top", top), ("bottom", bottom)):
        f = _ransac_line(pts, tol, rng)
        if f is None:
            return None, {"why": f"no {name} edge"}
        lines[name] = f[0]; rms.append(f[1]); share.append(f[2])
    # left/right: where do the top and bottom edges end? A phone held upright in front of a TV usually
    # cuts the TV off at the sides, and then the side of the picture is the side of the screen.
    def y_at(line, x):
        return -(line[0] * x + line[2]) / line[1] if abs(line[1]) > 1e-6 else cy
    ends = []
    for name, pts in (("top", top), ("bottom", bottom)):
        ln = lines[name]
        on = pts[np.abs(pts @ ln[:2] + ln[2]) < tol]
        ends.append((np.percentile(on[:, 0], 2), np.percentile(on[:, 0], 98)))
    ext_l, ext_r = min(e[0] for e in ends), max(e[1] for e in ends)
    ya = max(y_at(lines["top"], cx), 0); yb = min(y_at(lines["bottom"], cx), h - 1)
    span = yb - ya
    info["sides"] = []
    for name, ext, sign, border in (("left", ext_l, +1, 0.0), ("right", ext_r, -1, w - 1.0)):
        if abs(ext - border) <= 0.035 * w:                       # runs off the picture: use its edge
            lines[name] = np.array([1.0, 0.0, -border])
            info["sides"].append(f"{name}=frame")
            continue
        win = 0.06 * w
        pts = row_edge(ext - win, ext + win, sign, ya + 0.12 * span, yb - 0.12 * span)
        f = _ransac_line(pts, tol, rng)
        if f is not None and f[2] >= 0.35:
            lines[name] = f[0]; rms.append(f[1]); share.append(f[2])
            info["sides"].append(f"{name}=edge")
        else:                                                    # no clear edge: join the line ends
            pa = np.array([ext, y_at(lines["top"], ext)]); pb = np.array([ext, y_at(lines["bottom"], ext)])
            d = pb - pa
            nrm = np.array([-d[1], d[0]]) / max(np.hypot(*d), 1e-6)
            lines[name] = np.array([nrm[0], nrm[1], -nrm @ pa])
            info["sides"].append(f"{name}=ends")
    corners = [_intersect(lines["top"], lines["left"]), _intersect(lines["top"], lines["right"]),
               _intersect(lines["bottom"], lines["left"]), _intersect(lines["bottom"], lines["right"])]
    if any(c is None for c in corners):
        return None, {"why": "parallel"}
    q = np.array(corners)
    global _last
    _last = q
    info["rms"] = max(rms)
    info["share"] = min(share)
    margin = 0.02 * max(w, h)
    if (q[:, 0] < -margin).any() or (q[:, 0] > w - 1 + margin).any() or \
            (q[:, 1] < -margin).any() or (q[:, 1] > h - 1 + margin).any():
        return None, {"why": "cut off", **info}
    area = _quad_area(q)
    info["area"] = area / (w * h)
    top_len = np.linalg.norm(q[1] - q[0]); bot_len = np.linalg.norm(q[3] - q[2])
    left_len = np.linalg.norm(q[2] - q[0]); right_len = np.linalg.norm(q[3] - q[1])
    info["aspect"] = (top_len + bot_len) / max(left_len + right_len, 1)
    if not (0.04 <= info["area"] <= 0.97):
        return None, {"why": "size", **info}
    inside = _inside(q, h, w)
    info["mean"] = float(gf[inside].mean()) if inside.any() else 0.0
    info["contrast"] = float(gf[inside].mean() - gf[~inside].mean()) if (~inside).any() and inside.any() else 0.0
    ok = (info["share"] >= 0.35 and info["rms"] <= tol
          and 1.1 <= info["aspect"] <= 2.8 and info["contrast"] >= 20
          and min(top_len, bot_len) / max(top_len, bot_len) > 0.7
          and min(left_len, right_len) / max(left_len, right_len) > 0.7)
    if not ok:
        info["why"] = "shape"
    return (q if ok else None), info


# ── over time ───────────────────────────────────────────────────────────────

def _frames(path, info, fps, width, start=None, dur=None, hdr_vf=""):
    w = width
    h = int(round(w * info.height / info.width / 2)) * 2
    return media.gray_frames(path, fps, w, h, start, dur, pre=hdr_vf), w / info.width


def _rectify(frame: np.ndarray, q: np.ndarray, w: int = 64, h: int = 36) -> np.ndarray:
    """The screen inside quad q, pulled out to a small flat w x h image (bilinear corner blend, nearest pixel)."""
    u = (np.arange(w) + 0.5) / w
    v = (np.arange(h) + 0.5) / h
    uu, vv = np.meshgrid(u, v)
    tl, tr, bl, br = q
    pts = ((1 - uu)[..., None] * (1 - vv)[..., None] * tl + uu[..., None] * (1 - vv)[..., None] * tr
           + (1 - uu)[..., None] * vv[..., None] * bl + uu[..., None] * vv[..., None] * br)
    x = np.clip(np.rint(pts[..., 0]).astype(int), 0, frame.shape[1] - 1)
    y = np.clip(np.rint(pts[..., 1]).astype(int), 0, frame.shape[0] - 1)
    return frame[y, x]


def _track(path, info, fps, width, start=None, dur=None, keep_rect: bool = False) -> Track:
    if keep_rect:
        w = width
        h = int(round(w * info.height / info.width / 2)) * 2
        rgb = media.rgb_frames(path, fps, w, h, start, dur, pre=media.tonemap_chain(info))
        frames = (rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114).astype(np.uint8)
        scale = w / info.width
    else:
        frames, scale = _frames(path, info, fps, width, start, dur, media.tonemap_chain(info))
        rgb = None
    n = len(frames)
    quads = np.full((n, 4, 2), np.nan)
    found = np.zeros(n, dtype=bool)
    dark = np.zeros(n, dtype=bool)
    means = []
    for i, g in enumerate(frames):
        q, meta = find_quad(g)
        if q is not None:
            quads[i] = q / scale
            found[i] = True
            means.append(meta["mean"])
            dark[i] = meta.get("mean", 255) < 40
    times = (start or 0.0) + (np.arange(n) + 0.5) / fps
    tr = Track(filmed=False, times=times, quads=quads, found=found, dark=dark,
               src_w=info.width, src_h=info.height, share=float(found.mean()) if n else 0.0)
    if rgb is not None and found.any():
        filled = _fill_and_smooth(times, quads * scale, found, np.nanmedian(quads[found], axis=0) * scale, sigma=0)
        tr.rect = np.stack([_rectify(rgb[i], filled[i]) for i in range(n)])
        centers = filled.mean(axis=1)
        tr.shake = np.concatenate([[0.0], np.linalg.norm(np.diff(centers, axis=0), axis=1) / rgb.shape[2]])
    return tr


def _output_size(quads: np.ndarray) -> tuple[int, int]:
    tl, tr, bl, br = np.median(quads, axis=0)
    aspect = (np.linalg.norm(tr - tl) + np.linalg.norm(br - bl)) / max(np.linalg.norm(bl - tl) + np.linalg.norm(br - tr), 1)
    if 1.62 <= aspect <= 1.95:
        return 1920, 1080                     # a whole TV or monitor
    return int(np.clip(round(1080 * aspect / 2) * 2, 1300, 2520)), 1080      # part of one, or ultrawide


def analyse(path, info, cfg=None) -> Track | None:
    """Whole-clip look. Returns a Track with .filmed set, or None when this isn't worth checking."""
    sc = ((cfg.get("clips") or {}).get("screen") if cfg else None) or {}
    mode = str(sc.get("mode", "auto"))
    if mode == "off":
        return None
    tr = _track(path, info, ANALYSIS_FPS, 240, keep_rect=True)
    good = tr.quads[tr.found]
    if len(good) == 0:
        return tr if mode == "on" else None
    areas = np.array([_quad_area(q) / (info.width * info.height) for q in good])
    tr.filmed = mode == "on" or (tr.share >= 0.45 and float(np.median(areas)) <= 0.9)
    if tr.filmed:
        tr.out_w, tr.out_h = _output_size(good)
        tr.notes.append(f"screen found in {tr.share:.0%} of the clip, about {np.median(areas):.0%} of the picture")
    return tr


def _fill_and_smooth(times: np.ndarray, quads: np.ndarray, found: np.ndarray, fallback: np.ndarray,
                     sigma: float) -> np.ndarray:
    n = len(times)
    out = np.empty((n, 4, 2))
    if not found.any():
        out[:] = fallback
        return out
    idx = np.nonzero(found)[0]
    for c in range(4):
        for a in range(2):
            out[:, c, a] = np.interp(np.arange(n), idx, quads[idx, c, a])
    if sigma > 0 and n > 2:
        r = int(np.ceil(sigma * 3))
        k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
        k /= k.sum()
        padded = np.concatenate([np.repeat(out[:1], r, axis=0), out, np.repeat(out[-1:], r, axis=0)])
        sm = np.empty_like(out)
        for c in range(4):
            for a in range(2):
                sm[:, c, a] = np.convolve(padded[:, c, a], k, mode="valid")
        out = sm
    return out


def _inset(q: np.ndarray, amount: float = INSET) -> np.ndarray:
    c = q.mean(axis=0)
    return c + (q - c) * (1 - 2 * amount)


def _pw(times: np.ndarray, vals: np.ndarray, var: str) -> str:
    """Piecewise-linear ffmpeg expression of `var` (seconds) through (times, vals).
    Written as a flat sum of pieces, each switched on only inside its own interval, because ffmpeg's
    expression parser refuses nesting deeper than about 100 (nested if()s broke stretches over ~30 s)."""
    if len(times) == 1 or np.ptp(vals) < 0.5:
        return f"{float(np.mean(vals)):.1f}"
    terms = [f"lt({var},{times[0]:.3f})*{vals[0]:.1f}"]
    for i in range(len(times) - 1):
        t0, t1, v0, v1 = times[i], times[i + 1], vals[i], vals[i + 1]
        if abs(v1 - v0) < 0.05:
            piece = f"{v0:.1f}"
        else:
            piece = f"({v0:.1f}+({v1 - v0:.2f})*({var}-{t0:.3f})/{t1 - t0:.3f})"
        terms.append(f"gte({var},{t0:.3f})*lt({var},{t1:.3f})*{piece}")
    terms.append(f"gte({var},{times[-1]:.3f})*{vals[-1]:.1f}")
    return "+".join(terms)


def segment_filter(path, info, track: Track, start: float, dur: float, fps: int = 30,
                   still: bool = False) -> str:
    """Filter chain (fps, perspective, scale) that turns one stretch of the clip into a flat screen.
    `still`: the input is a single frame taken at `start` (freeze frames)."""
    fallback = track.median_quad(start - 2, start + dur + 2)
    if fallback is None:
        fallback = track.median_quad()
    if still or dur < 0.6:
        fine = _track(path, info, FINE_FPS, 480, max(0.0, start - 0.4), 0.8)
        q = np.median(fine.quads[fine.found], axis=0) if fine.found.any() else fallback
        q = _inset(q)
        persp = ":".join(f"{k}={v:.1f}" for k, v in zip(("x0", "y0", "x1", "y1", "x2", "y2", "x3", "y3"), q.reshape(-1)))
        pre = "" if still else f"fps={fps},"
        return f"{pre}perspective={persp}:interpolation=cubic,scale={track.out_w}:{track.out_h}:flags=lanczos,setsar=1"
    fine = _track(path, info, FINE_FPS, 480, start, dur)
    sm = _fill_and_smooth(fine.times - start, fine.quads, fine.found, fallback, sigma=1.3)
    # keyframes at KEY_FPS over the smoothed track
    key_fps = min(KEY_FPS, 400.0 / max(dur, 1.0))       # long videos: fewer keys (the track is smoothed anyway)
    kt = np.arange(0.0, dur + 1e-6, 1.0 / key_fps)
    ft = fine.times - start
    keys = np.empty((len(kt), 4, 2))
    for c in range(4):
        for a in range(2):
            keys[:, c, a] = np.interp(kt, ft, sm[:, c, a]) if len(ft) else fallback[c, a]
    keys = np.array([_inset(q) for q in keys])
    var = f"(in/{fps})"
    parts = []
    for c, (xn, yn) in enumerate((("x0", "y0"), ("x1", "y1"), ("x2", "y2"), ("x3", "y3"))):
        parts.append(f"{xn}='{_pw(kt, keys[:, c, 0], var)}'")
        parts.append(f"{yn}='{_pw(kt, keys[:, c, 1], var)}'")
    return (f"fps={fps},perspective={':'.join(parts)}:interpolation=cubic:eval=frame,"
            f"scale={track.out_w}:{track.out_h}:flags=lanczos,setsar=1")


def static_filter(track: Track, t0: float | None = None, t1: float | None = None, width: int | None = None) -> str:
    """One fixed perspective for quick analysis passes (tone, motion)."""
    q = track.median_quad(t0, t1)
    if q is None:
        return ""
    q = _inset(q)
    persp = ":".join(f"{k}={v:.1f}" for k, v in zip(("x0", "y0", "x1", "y1", "x2", "y2", "x3", "y3"), q.reshape(-1)))
    out = f"perspective={persp}"
    if width:
        out += f",scale={width}:{int(width * track.out_h / track.out_w) // 2 * 2}"
    return out
