"""The activity orb: what the agent is doing, as motion rather than a spinner.

The bar used to show a static logo mark with a small badge glyph stuck to its
corner -- a breathing dot, a rotating arc, a tick. That said "something is
happening" and never said *what*. This module replaces both with a dotted 3D
orb whose motion differs per activity: a waveform rolls through the rings
while it listens, particles run tilted orbits while it works, a sash
undulates while it pastes.

Ported from Thinking Orbs by Jakub Antalik -- see the notice at the end of
this docstring. The port is geometry-exact rather than an impression:
upstream publishes ``spec/orbs-golden.json``, frozen dot lists at fixed
timestamps, precisely so native ports can be verified as numbers, and
``tests/test_orb.py`` checks this implementation against those vectors.

**Why a port and not the package.** ``thinking-orbs`` is a React component
rendering into an HTML canvas. The bar is a tkinter window with every pixel
composited by PIL, and :mod:`sway.bar` rules out a webview surface for a
functional reason rather than a stylistic one: a webview window activates
when shown, and paste targets the *focused* window, so a webview bar would
swallow the user's dictation. The engine is plain 2D arcs -- dots positioned
as a function of time -- which is exactly what ``ImageDraw.ellipse`` draws.

**Shape of this module.** Pure: no Tk, no global clock, no wall-clock read.
Frames are a function of ``(variant, size, phase)``, sampled by the caller
once per repaint -- the same idiom as :mod:`sway.motion`, and for the same
reason: a dropped frame or a stalled poll loop cannot desynchronise anything,
and the whole thing is unit-testable without a screen.

**Two deliberate departures from upstream**, both defaulted off so the golden
vectors still pass:

* ``amplitude`` scales the ``wave`` mode's undulation, so while the user is
  actually speaking the orb moves with their voice. It is driven by the mic
  level the bar already measures -- a real signal, not a decorative one.
* Colour. Upstream paints greyscale and mirrors the ink on dark substrates.
  We keep its depth language exactly -- near dots bright, far dots sunk --
  but run the resulting value along a ramp from the state's accent to
  :data:`sway.branding.INK`, so hue carries the state as well as motion does.
  With a grey accent the ramp degenerates to upstream's own greyscale.

----

Thinking Orbs: https://github.com/Jakubantalik/thinking-orbs
Demo:          https://orbs.jakubantalik.com/

MIT License

Copyright (c) 2026 Jakub Antalik

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

from __future__ import annotations

import math
from enum import StrEnum
from functools import lru_cache

from PIL import Image, ImageDraw

__all__ = [
    "MODE_FRAMES",
    "PRESETS",
    "REDUCED_MOTION_PHASE",
    "STATE_TO_MODE",
    "Dot",
    "OrbVariant",
    "frame",
    "orb_image",
    "preset_size_for",
    "resolve_preset",
]


# ---------------------------------------------------------------------------
# Variants
# ---------------------------------------------------------------------------
#
# Upstream ships nine. Four are carried here -- the four the engine can
# honestly report, per CLAUDE.md's rule that the bar never claims a state it
# is not in. Adding a fifth means adding the stage that justifies it first.


class OrbVariant(StrEnum):
    """An activity the agent can actually be in."""

    #: The hotkey is held and audio is being captured.
    LISTENING = "listening"
    #: A request is in flight -- transcription, cleanup, or a transform.
    WORKING = "working"
    #: Text is being placed at the cursor.
    COMPOSING = "composing"
    #: At rest. Present so the resting capsule has a mark rather than a hole.
    BREATHING = "breathing"


#: Upstream's state -> mode table, restricted to the variants above. Two
#: states share a painter (``composing`` and ``breathing`` are both
#: :func:`_frame_ribbon`, separated by ``faceOn``), which is why the names are
#: kept distinct from the modes.
STATE_TO_MODE: dict[str, str] = {
    "listening": "wave",
    "working": "orbits",
    "composing": "ribbon",
    "breathing": "ring",
}

#: Upstream renders this single frame under ``prefers-reduced-motion``. Not
#: t=0: every mode is mid-gesture here, so the static frame looks composed
#: rather than like an animation caught before it started.
REDUCED_MOTION_PHASE = 0.6

#: Which tuned preset each variant borrows.
#:
#: Upstream ships two -- 64 for a chat avatar, 20 for inline text -- and is
#: explicit that they are separately tuned rather than one design scaled,
#: because a density that reads at 64px turns to mud at 20px. The bar's slot
#: is 34px, between the two, so each variant takes the preset that survives
#: at 34 rather than all four taking the nearer number:
#:
#: * ``working`` and ``listening`` take **64**. At the 20 preset ``working``
#:   resolves to three orbits and reads as four stray dots rather than as a
#:   system, and ``listening``'s globe loses the ring structure the waveform
#:   travels through.
#: * ``composing`` and ``breathing`` take **20**. Their 64 presets pack 484
#:   and 566 dots into 34px, where the lanes merge into a solid blob and the
#:   ring's outline goes to mush -- and cost 2.3ms a frame to draw.
_VARIANT_PRESET: dict[str, int] = {
    "listening": 64,
    "working": 64,
    "composing": 20,
    "breathing": 20,
}


def preset_size_for(variant: OrbVariant | str) -> int:
    """The tuned preset :data:`_VARIANT_PRESET` picks for ``variant``."""
    return _VARIANT_PRESET[str(variant)]


# ---------------------------------------------------------------------------
# Core math
# ---------------------------------------------------------------------------
#
# Transliterated from upstream ``src/engine/core.ts``. More explicit than
# idiomatic Python would be, deliberately: every operation has to land on the
# same IEEE-754 double the JS does, or the golden vectors stop matching.

#: One dot, as upstream's stride-6 record: x, y, z, radius, ink, alpha.
#: A tuple rather than a dataclass because a busy frame is a few hundred of
#: these rebuilt every repaint, and this sits inside the latency budget.
Dot = tuple[float, float, float, float, float, float]


def _js_round(value: float) -> int:
    """JavaScript's ``Math.round``: halves go up, never to even.

    Python's :func:`round` is banker's rounding, so ``round(0.5)`` is 0 where
    JS gives 1. Every count in :func:`_scale_counts` runs through here; using
    the builtin instead silently changes the dot count on some presets.
    """
    return math.floor(value + 0.5)


def _hash_d(a: float, b: float) -> float:
    """Deterministic hash in [0, 1)."""
    h = math.sin(a * 12.9898 + b * 78.233) * 43758.5453
    return h - math.floor(h)


def _fib_dir(i: int, n: int) -> tuple[float, float, float]:
    """Stable directions on a unit sphere (Fibonacci lattice)."""
    golden = math.pi * (3 - math.sqrt(5))
    y = 1 - (2 * (i + 0.5)) / n
    rad = math.sqrt(1 - y * y)
    a = i * golden
    return (rad * math.cos(a), y, rad * math.sin(a))


def _make_proj(yaw: float, tilt: float, cx: float, cy: float, scale: float):
    """Shared spin + tilt + orthographic projection."""
    st = math.sin(tilt)
    ct = math.cos(tilt)
    sy = math.sin(yaw)
    cyw = math.cos(yaw)

    def project(x: float, y: float, z: float) -> tuple[float, float, float]:
        x1 = x * cyw + z * sy
        z1 = -x * sy + z * cyw
        y1 = y * ct - z1 * st
        z2 = y * st + z1 * ct
        return (cx + x1 * scale, cy - y1 * scale, z2)

    return project


def _radius_scale(size: float, pow_: float) -> float:
    """Radii were tuned for a 300pt frame; sub-linear, so small orbs stay legible."""
    return (size / 300.0) ** pow_


def _finalize(dots: list[Dot], r_min: float) -> list[Dot]:
    """Drop invisible marks, floor the radii, z-sort far to near.

    Upstream runs this in the geometry step rather than the painter, so a
    frame is a *complete* set of draw instructions -- every value final, the
    list already in draw order. That is what makes the output portable, and
    what lets the tests compare numbers instead of pixels.
    """
    visible = [
        (x, y, z, r if r > r_min else r_min, white, a)
        for (x, y, z, r, white, a) in dots
        if a >= 0.02
    ]
    visible.sort(key=lambda dot: dot[2])
    return visible


# ---------------------------------------------------------------------------
# Profiles and presets
# ---------------------------------------------------------------------------
#
# Transcribed from upstream ``src/engine/profiles.ts`` and ``src/presets.ts``,
# cross-checked against the ``spec/orbs-spec.json`` the project publishes for
# exactly this purpose. Only the four modes we use are carried.

_BASE_PROFILES: dict[str, dict[str, float]] = {
    "orbits": {
        "orbitN": 12, "ghostN": 40, "ghostR": 0.9, "ghostA": 0.5,
        "particles": 3, "partR": 1.2, "partRDepth": 1.6,
        "rsPow": 0.6, "rMin": 0.3,
    },
    "wave": {
        "rings": 15, "lonDensity": 40, "rBase": 0.6, "rDepth": 1.7,
        "rsPow": 0.6, "rMin": 0.3,
    },
    "ribbon": {
        "lanes": 5, "segs": 88, "ghostN": 150, "rBase": 1.1, "rDepth": 1.7,
        "rsPow": 0.6, "rMin": 0.3,
    },
    "ring": {
        "lanes": 5, "segs": 88, "ghostN": 0, "faceOn": 1, "rBase": 1.1,
        "rDepth": 1.7, "rsPow": 0.6, "rMin": 0.3,
    },
}

#: ``{mode: {preset_size: (speed, count, size, extra)}}``. Upstream ships two
#: tuned sizes rather than one design scaled, because a dot density that reads
#: at 64px turns to mud at 20px.
PRESETS: dict[str, dict[int, tuple[float, float, float, dict[str, float]]]] = {
    "orbits": {
        64: (1.885, 1.0, 1.0, {}),
        20: (3.9, 0.238, 2.4, {}),
    },
    "wave": {
        64: (4.388, 0.341, 1.0, {}),
        20: (3.998, 0.105, 1.6, {}),
    },
    "ribbon": {
        64: (2.34, 0.25, 0.85, {"spin": 0, "bandMul": 3.9, "wobMul": 1}),
        20: (3.12, 0.051, 1.073, {"spin": 0, "bandMul": 4.94, "wobMul": 1}),
    },
    "ring": {
        64: (3.24, 0.25, 0.956, {"spin": 0, "bandMul": 3.627, "wobMul": 0.368}),
        20: (3.78, 0.028, 1.622, {"spin": 0, "bandMul": 3.968, "wobMul": 0.565}),
    },
}

#: 2-D lattices (rings x dots-per-ring) come in pairs -- each side takes the
#: square root so the *total* scales by ``count``. A key joins at most one pair.
_COUNT_PAIRS = (("latRings", "lonDensity"), ("rings", "lonDensity"), ("lanes", "segs"))
_COUNT_KEYS = ("orbitN", "ghostN", "nodeN", "strandN", "signals")
_RADIUS_KEYS = (
    "rBase", "rDepth", "rActive", "rDot", "ghostR", "partR", "partRDepth",
    "nodeR", "nodeRDepth",
)


def _scale_counts(opts: dict[str, float], scale: float) -> dict[str, float]:
    out = dict(opts)
    done: set[str] = set()
    rt = math.sqrt(scale)
    for a, b in _COUNT_PAIRS:
        if a in out and b in out and a not in done and b not in done:
            out[a] = max(2, _js_round(out[a] * rt))
            out[b] = max(2, _js_round(out[b] * rt))
            done.add(a)
            done.add(b)
    for key in _COUNT_KEYS:
        value = out.get(key)
        # An explicit 0 means the mode opted out of that layer entirely (ring
        # has no ghost sphere); scaling must not resurrect it as a stray dot.
        if value is not None and value != 0 and key not in done:
            out[key] = max(1, _js_round(value * scale))
    return out


def _scale_radii(opts: dict[str, float], scale: float) -> dict[str, float]:
    out = dict(opts)
    for key in _RADIUS_KEYS:
        if key in out:
            out[key] = out[key] * scale
    out["rSizeMul"] = out.get("rSizeMul", 1.0) * scale
    return out


@lru_cache(maxsize=32)
def resolve_preset(variant: str, preset_size: int) -> tuple[str, float, tuple]:
    """``(mode, speed, opts)`` for a variant at one of the tuned sizes.

    ``opts`` comes back as a sorted tuple of pairs rather than a dict so this
    can be cached; :func:`frame` turns it back into a mapping. Resolution is
    upstream's: scale the counts, scale the radii, merge the extras verbatim.
    """
    mode = STATE_TO_MODE[str(variant)]
    speed, count, size_mul, extra = PRESETS[mode][preset_size]
    opts = dict(_BASE_PROFILES[mode])
    if count != 1:
        opts = _scale_counts(opts, count)
    if size_mul != 1:
        opts = _scale_radii(opts, size_mul)
    opts.update(extra)
    return mode, speed, tuple(sorted(opts.items()))


# ---------------------------------------------------------------------------
# Mode painters
# ---------------------------------------------------------------------------


def _frame_orbits(size: float, t: float, o: dict[str, float]) -> list[Dot]:
    """Particles on tilted orbits -- *working*.

    No nucleus: the tuned preset runs coreless, so it is ghost paths and the
    particles doing the work.
    """
    cx = cy = size / 2
    big_r = (size / 2) * 0.82
    pt = _make_proj(t * 0.12, 0.3, cx, cy, 1)
    rs = _radius_scale(size, o.get("rsPow", 0.6))

    dots: list[Dot] = []
    orbit_n = int(o.get("orbitN", 12))
    ghost_n = int(o.get("ghostN", 40))
    particles = int(o.get("particles", 3))
    ghost_r = o.get("ghostR", 0.9)
    ghost_a = o.get("ghostA", 0.5)
    part_r = o.get("partR", 1.2)
    part_r_depth = o.get("partRDepth", 1.6)

    for orb in range(orbit_n):
        h1 = _hash_d(orb, 1.7)
        h2 = _hash_d(orb, 5.2)
        h3 = _hash_d(orb, 8.9)
        ro = big_r * (0.45 + 0.52 * h1)
        th = h1 * 2 * math.pi
        phi = math.acos(2 * h2 - 1)
        # orbit plane basis (u, v perpendicular to the normal n)
        nx = math.sin(phi) * math.cos(th)
        ny = math.cos(phi)
        nz = math.sin(phi) * math.sin(th)
        ux = -ny
        uy = nx
        uz = 0.0
        ul = max(1e-6, math.sqrt(ux * ux + uy * uy))
        ux /= ul
        uy /= ul
        vx = ny * uz - nz * uy
        vy = nz * ux - nx * uz
        vz = nx * uy - ny * ux
        speed = (0.25 + 0.55 * h3) * (1 if h3 > 0.5 else -1)

        # the ghost path
        for k in range(ghost_n):
            a = (k / ghost_n) * 2 * math.pi
            ca = math.cos(a)
            sa = math.sin(a)
            px, py, z = pt((ux * ca + vx * sa) * ro, (uy * ca + vy * sa) * ro,
                           (uz * ca + vz * sa) * ro)
            depth = (z / ro + 1) / 2
            dots.append((px, py, z, ghost_r * rs, 0.72, ghost_a * (0.4 + 0.6 * depth)))

        # the particles doing the work
        for m in range(particles):
            a = t * speed + (m / particles) * 2 * math.pi + h2 * 6
            ca = math.cos(a)
            sa = math.sin(a)
            px, py, z = pt((ux * ca + vx * sa) * ro, (uy * ca + vy * sa) * ro,
                           (uz * ca + vz * sa) * ro)
            depth = (z / ro + 1) / 2
            dots.append((px, py, z, (part_r + part_r_depth * depth) * rs,
                         0.3 - 0.22 * depth, 1.0))

    return _finalize(dots, o.get("rMin", 0.3))


def _frame_wave(size: float, t: float, o: dict[str, float]) -> list[Dot]:
    """A waveform rolls through the rings -- *listening*.

    ``amplitude`` is ours, not upstream's: it scales the undulation so the orb
    moves with the speaker's actual voice level. It defaults to 1.0, which is
    upstream's geometry exactly.
    """
    cx = cy = size / 2
    # 0.76 base x 1.15 -- the undulation pulls the sphere inward, so wave read
    # ~15% smaller than the other lattice modes; scaled up to match them.
    big_r = (size / 2) * 0.874
    pt = _make_proj(t * 0.18, 0.38, cx, cy, 1)
    rs = _radius_scale(size, o.get("rsPow", 0.6))

    dots: list[Dot] = []
    rings = int(o.get("rings", 15))
    lon_density = o.get("lonDensity", 40)
    r_base = o.get("rBase", 0.6)
    r_depth = o.get("rDepth", 1.7)
    amplitude = o.get("amplitude", 1.0)

    for ri in range(rings + 1):
        lat = -math.pi / 2 + (ri / rings) * math.pi
        cos_lat = math.cos(lat)
        sin_lat = math.sin(lat)
        # two waves, different tempi -- organic, never quite repeating
        w = 0.62 * math.sin(t * 2.1 - ri * 0.52) + 0.38 * math.sin(t * 1.27 + ri * 0.83)
        rr = big_r * (0.88 + 0.105 * w * amplitude)
        lon_count = max(1, _js_round(abs(cos_lat) * lon_density))
        for lj in range(lon_count):
            lon = (lj / lon_count) * 2 * math.pi
            px, py, z = pt(cos_lat * math.cos(lon) * rr, sin_lat * rr,
                           cos_lat * math.sin(lon) * rr)
            depth = (z / big_r + 1) / 2
            crest = max(0.0, w)
            dots.append((px, py, z,
                         (r_base + r_depth * depth) * (1 + 0.4 * crest) * rs,
                         0.66 - 0.56 * depth - 0.1 * crest, 1.0))

    return _finalize(dots, o.get("rMin", 0.3))


def _frame_ribbon(size: float, t: float, o: dict[str, float]) -> list[Dot]:
    """An undulating sash rides a great circle -- *composing*.

    The same painter drives *breathing* via ``faceOn``: a face-on circle whose
    radius, rather than its out-of-plane offset, undulates, so it reads as a
    ring slowly morphing instead of a sash in orbit.
    """
    cx = cy = size / 2
    big_r = (size / 2) * 0.78
    # spin scales the 3D tumble; spin=0 freezes the band's orientation,
    # leaving only the traveling undulation
    spin = o.get("spin", 1)
    cam_tilt = 0.3
    pt = _make_proj(t * 0.1 * spin, cam_tilt, cx, cy, 1)
    rs = _radius_scale(size, o.get("rsPow", 0.6))
    face_on = bool(o.get("faceOn", 0))

    dots: list[Dot] = []
    ghost_n = int(o.get("ghostN", 150))
    for i in range(ghost_n):
        d = _fib_dir(i, ghost_n)
        px, py, z = pt(d[0] * big_r, d[1] * big_r, d[2] * big_r)
        depth = (z / big_r + 1) / 2
        dots.append((px, py, z, 0.8 * rs, 0.78, 0.1 + 0.22 * depth))

    # The band plane, precessing (frozen when spin=0). The projection squashes
    # the great circle vertically by cos(ta + camTilt); face-on sets
    # ta = -camTilt so that term is 1 and the band reads as a true circle
    # rather than ribbon's tilted ellipse.
    ya = t * 0.24 * spin
    ta = -cam_tilt if face_on else 0.55 + 0.3 * math.sin(t * 0.18) * spin
    ux = math.cos(ya)
    uy = 0.0
    uz = math.sin(ya)
    vx = -uz * math.sin(ta)
    vy = math.cos(ta)
    vz = ux * math.sin(ta)
    # plane normal n = u x v
    nx = uy * vz - uz * vy
    ny = uz * vx - ux * vz
    nz = ux * vy - uy * vx

    # Radial lobes swell past R, so the base radius is pulled in by most of
    # the wobble amplitude: the silhouette then stays inside the frame however
    # far the deformation is pushed.
    wob_mul = o.get("wobMul", 1)
    wob_amp = 0.23 * wob_mul
    base_r = big_r / (1 + 0.85 * wob_amp) if face_on else big_r

    base_lanes = o.get("lanes", 5)
    segs = int(o.get("segs", 88))
    lanes = max(1, _js_round(base_lanes * o.get("bandMul", 1)))
    r_base = o.get("rBase", 1.1)
    r_depth = o.get("rDepth", 1.7)

    for w_i in range(lanes):
        lane_off = (w_i - (lanes - 1) / 2) * 0.075
        edge = abs(w_i - (lanes - 1) / 2) / max(1, (lanes - 1) / 2)
        for k in range(segs):
            a = (k / segs) * 2 * math.pi
            # the undulation: two traveling waves along the band
            wob = (0.16 * math.sin(a * 3 - t * 1.7 + w_i * 0.22)
                   + 0.07 * math.sin(a * 5 + t * 1.1)) * wob_mul
            # A normal-direction wobble is cancelled by the re-normalisation
            # below -- the point lands back on the sphere, so the silhouette
            # is pinned at R and the deformation can only pull dots inward.
            # Face-on instead modulates the in-plane RADIUS, so lobes genuinely
            # swell outward and pinch inward.
            radial = 1 + wob if face_on else 1
            off = lane_off if face_on else lane_off + wob
            ca = math.cos(a)
            sa = math.sin(a)
            x = ux * ca + vx * sa + nx * off
            y = uy * ca + vy * sa + ny * off
            z = uz * ca + vz * sa + nz * off
            length = math.sqrt(x * x + y * y + z * z)
            rr = base_r * radial
            px, py, zr = pt((x / length) * rr, (y / length) * rr, (z / length) * rr)
            depth = (zr / big_r + 1) / 2
            dots.append((px, py, zr,
                         (r_base + r_depth * depth) * (1 - 0.25 * edge) * rs,
                         0.52 - 0.44 * depth + 0.18 * edge,
                         0.4 + 0.6 * depth))

    return _finalize(dots, o.get("rMin", 0.3))


#: Mode name -> the function that draws it. ``ring`` and ``ribbon`` share a
#: painter; ``faceOn`` in the opts is what separates them.
MODE_FRAMES = {
    "orbits": _frame_orbits,
    "wave": _frame_wave,
    "ribbon": _frame_ribbon,
    "ring": _frame_ribbon,
}


def frame(
    variant: OrbVariant | str,
    size: float,
    phase_s: float,
    *,
    preset_size: int | None = None,
    speed: float = 1.0,
    amplitude: float = 1.0,
) -> list[Dot]:
    """One instant, as a finished list of dots in draw order.

    ``phase_s`` is elapsed seconds; the preset's own tempo and ``speed`` are
    applied here, so callers hand over wall-clock time and nothing else.
    ``preset_size`` defaults to :func:`preset_size_for`; pass 64 or 20
    explicitly to check a frame against upstream's golden vectors.
    """
    if preset_size is None:
        preset_size = preset_size_for(variant)
    mode, preset_speed, opts_items = resolve_preset(str(variant), preset_size)
    opts = dict(opts_items)
    if amplitude != 1.0:
        opts["amplitude"] = amplitude
    return MODE_FRAMES[mode](size, phase_s * preset_speed * speed, opts)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


def orb_image(
    variant: OrbVariant | str,
    size: int,
    *,
    phase_s: float = 0.0,
    accent: tuple[int, int, int] = (235, 236, 240),
    ink: tuple[int, int, int] = (235, 236, 240),
    opacity: float = 1.0,
    amplitude: float = 1.0,
    speed: float = 1.0,
    preset_size: int | None = None,
    supersample: int = 1,
) -> Image.Image:
    """One orb frame as RGBA, ``size * supersample`` px square.

    Geometry is computed at ``size`` -- the size the orb is actually *seen*
    at -- and only the drawing is supersampled, which is the same separation
    upstream makes between CSS pixels and device pixels. Computing it at the
    supersampled size instead would thin the dot field out, because upstream's
    radii scale sub-linearly with the frame.

    Colour follows upstream's dark-substrate rule -- ink mirrored, so near
    dots read bright -- and then runs that value from ``accent`` at the far
    end to ``ink`` at the near end.
    """
    if size < 1:
        raise ValueError("size must be >= 1")
    if supersample < 1:
        raise ValueError("supersample must be >= 1")

    canvas = Image.new("RGBA", (size * supersample, size * supersample), (0, 0, 0, 0))
    opacity = _clamp01(opacity)
    if opacity <= 0.0:
        return canvas
    draw = ImageDraw.Draw(canvas)

    ar, ag, ab = accent
    ir, ig, ib = ink
    for x, y, _z, r, white, a in frame(
        variant, size, phase_s, preset_size=preset_size, speed=speed, amplitude=amplitude
    ):
        # Upstream: g = round((dark ? 1 - white : white) * 255). The bar is a
        # dark substrate, so the ink is mirrored and `lum` runs 0 (sunk) to
        # 1 (near).
        lum = _clamp01(1.0 - white)
        alpha = round(_clamp01(a) * opacity * 255)
        if alpha <= 0:
            continue
        fill = (
            round(ar + (ir - ar) * lum),
            round(ag + (ig - ag) * lum),
            round(ab + (ib - ab) * lum),
            alpha,
        )
        cx = x * supersample
        cy = y * supersample
        rr = r * supersample
        draw.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), fill=fill)

    return canvas
