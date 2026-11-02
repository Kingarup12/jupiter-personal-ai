"""Jupiter computer-use overlay art (pure Pillow rendering + deterministic animation; no Win32, fully testable).

Design source: prototype/cursor_demo/index.html (Cosmic Precision Cursor) — stealth arrow with a cyan edge and a
Jovian band inset, the "Great Red Spot" vortex at the pointer core, a tilted orbital ring with a Galilean moon, a
JUPITER status badge, an ion-stardust trail while gliding and a gravitational shockwave on each action.

The cursor is an HONEST action marker: it shows where Jupiter acted. It never is, and never moves, the user's real
mouse pointer (background control uses UI Automation; Windows has a single system pointer).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from functools import lru_cache

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

CYAN = (0, 210, 255)
ELECTRIC = (0, 102, 255)
AMBER = (255, 145, 66)
RED = (255, 59, 48)
WHITE = (255, 255, 255)
GREY = (148, 163, 184)

# mode -> (edge colour, glow colour, badge text, badge dot colour)
# Marker 212 (user, 24 Oct: "in computer use it should flash blue light, not orange"): every active state is Jupiter
# blue; "needs you" is a brighter blue (and its words), never orange.
BLUE = (79, 172, 254)
MODES = {
    "observing": (CYAN, ELECTRIC, "JUPITER // OBSERVING", CYAN),
    "controlling": (CYAN, ELECTRIC, "JUPITER // ACTING", CYAN),
    "paused": (GREY, (90, 100, 120), "JUPITER // PAUSED", GREY),
    "attention": (BLUE, ELECTRIC, "JUPITER // NEEDS YOU", BLUE),
    "sharing": (CYAN, ELECTRIC, "JUPITER // SCREEN SHARED", CYAN),
    # Marker 218: the user took the mouse; Jupiter waits and continues when they are idle (amber, not blue).
    "yield": (AMBER, (255, 176, 46), "JUPITER // WAITING FOR YOU", AMBER),
}
ACTION_LABELS = {"click": "JUPITER // CLICK", "pointer": "JUPITER // CLICK (MOUSE)", "type": "JUPITER // TYPING",
                 "key": "JUPITER // KEY", "scroll": "JUPITER // SCROLL"}

GLOW_MARGIN = 34          # pixels of glow outside the controlled window
CURSOR_BOX = (360, 240)   # cursor overlay size; the pointer tip (hotspot) sits at HOTSPOT inside it
HOTSPOT = (120, 110)
CURSOR_SCALE = 1.3        # arrow/ring/vortex size relative to the prototype's 32 px arrow


def _font(size: int):
    for name in ("consola.ttf", "CascadiaMono.ttf", "segoeui.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


# ── edge glow (rendered once per size/mode; breathing is done by the window's constant alpha) ─────────────────
# Marker 217 (user, 25 Oct: "it shouldn't be a thin strip of light blue at the border - a little more, coming out"):
# a wide luminous Jupiter-blue haze (~150 px) instead of a ~50 px dim band, brightest at the edge.
SCREEN_GLOW_BAND = 75     # how far the screen-edge glow reaches inward from each monitor edge (half-resolution px)
GLOW_HAZE = (30, 120, 255)
GLOW_MID = (64, 168, 255)
GLOW_CORE = (150, 214, 255)


@lru_cache(maxsize=6)
def render_screen_glow(bounds: tuple, monitors: tuple, mode: str = "observing") -> Image.Image:
    """Desktop-frame glow (Codex 165 design: all monitor edges, independent of the target window): a neon haze that
    fades inward from every monitor's edges; everything else is fully transparent. Computed at half resolution."""
    edge, glow, *_ = MODES.get(mode, MODES["observing"])
    haze, mid, core = ((glow, edge, edge) if mode == "paused" else
                       ((235, 130, 20), (255, 176, 46), (255, 222, 150)) if mode == "yield" else
                       (GLOW_HAZE, GLOW_MID, GLOW_CORE))
    left, top, right, bottom = bounds
    width, height = max(1, right - left), max(1, bottom - top)
    half = Image.new("RGBA", (max(1, width // 2), max(1, height // 2)), (0, 0, 0, 0))
    for stroke, blur, colour, strength in ((70, 34, haze, 235), (26, 13, mid, 245), (8, 4, core, 255), (3, 1, core, 255)):
        # only the ALPHA is blurred: blurring colour into transparent black turned the blue a muddy navy
        shape = Image.new("L", half.size, 0)
        draw = ImageDraw.Draw(shape)
        for m_left, m_top, m_right, m_bottom in monitors:
            box = ((m_left - left) // 2, (m_top - top) // 2, (m_right - left) // 2 - 1, (m_bottom - top) // 2 - 1)
            draw.rectangle(box, outline=strength, width=stroke)
        blurred = Image.new("RGBA", half.size, colour + (0,))
        blurred.putalpha(shape.filter(ImageFilter.GaussianBlur(blur)))
        for _ in range(2 if stroke > 3 else 1):
            half = Image.alpha_composite(half, blurred)
    # Smooth inward falloff (no hard cut): full strength at the edge, fading to nothing SCREEN_GLOW_BAND*2 inward.
    reach = max(2, SCREEN_GLOW_BAND)                              # in half-resolution pixels
    mask = Image.new("L", half.size, 0)
    mask_draw = ImageDraw.Draw(mask)
    for m_left, m_top, m_right, m_bottom in monitors:
        box = [(m_left - left) // 2, (m_top - top) // 2, (m_right - left) // 2 - 1, (m_bottom - top) // 2 - 1]
        mask_draw.rectangle(box, outline=255, width=reach // 3)
    mask = mask.filter(ImageFilter.GaussianBlur(reach * 0.3))      # rounded, seamless corners (no mitre lines)
    half.putalpha(ImageChops.multiply(half.getchannel("A"), mask))
    image = half.resize((width, height), Image.BILINEAR)
    draw = ImageDraw.Draw(image)
    rim = GREY if mode == "paused" else (210, 245, 255)
    for m_left, m_top, m_right, m_bottom in monitors:
        draw.rectangle((m_left - left, m_top - top, m_right - left - 1, m_bottom - top - 1), outline=rim + (200,), width=2)
    return image


# ── cursor sprite ─────────────────────────────────────────────────────────────────────────────────────────────────
_OUTER = [(3, 2), (26, 15), (16, 18), (22, 30), (16, 33), (10, 21), (3, 26)]
_INNER = [(5, 6), (20, 15), (14, 17), (19, 27), (16, 28), (11, 19), (6, 22)]


def _scaled(points, scale, origin):
    ox, oy = origin
    return [(ox + x * scale, oy + y * scale) for x, y in points]


def _geometry(press: float = 0.0, s: int = 1):
    scale = (32 / 36) * CURSOR_SCALE * s * (1.0 - 0.12 * press)
    hx, hy = HOTSPOT[0] * s, HOTSPOT[1] * s
    origin = (hx - 3 * scale, hy - 2 * scale)                    # path point (3,2) is the pointer tip
    return scale, origin, (origin[0] + 16 * scale, origin[1] + 16 * scale)


@lru_cache(maxsize=8)
def _cursor_base(dim: bool, press_bucket: int) -> Image.Image:
    """Arrow (glow, body, Jovian band, rim, apex line) + tilted orbital ring: drawn ONCE, supersampled."""
    s = 3
    press = press_bucket / 4
    w, h = CURSOR_BOX[0] * s, CURSOR_BOX[1] * s
    scale, origin, (cx, cy) = _geometry(press, s)
    art = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(glow).polygon(_scaled(_OUTER, scale, origin), fill=CYAN + (200,))
    art = Image.alpha_composite(art, glow.filter(ImageFilter.GaussianBlur(7 * s)))
    draw = ImageDraw.Draw(art)
    draw.polygon(_scaled(_OUTER, scale, origin), fill=(5, 10, 24, 235))
    band = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    band_draw = ImageDraw.Draw(band)
    inner = _scaled(_INNER, scale, origin)
    band_draw.polygon(inner, fill=CYAN + (120,))                  # Jovian band inset (gradient approximated)
    band_draw.polygon(inner[3:6], fill=AMBER + (90,))
    art = Image.alpha_composite(art, band)
    draw = ImageDraw.Draw(art)
    draw.line(_scaled(_OUTER + _OUTER[:1], scale, origin), fill=(220, 248, 255, 255), width=max(1, int(1.8 * s)),
              joint="curve")
    draw.line(_scaled([(3, 2), (15, 17)], scale, origin), fill=WHITE + (230,), width=max(1, int(1.2 * s)))
    rx, ry = 22 * CURSOR_SCALE * s, 9 * CURSOR_SCALE * s
    ring = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(ring).ellipse((cx - rx, cy - ry, cx + rx, cy + ry), outline=CYAN + (130,), width=max(1, s))
    art = Image.alpha_composite(art, ring.rotate(-15, center=(cx, cy), resample=Image.BICUBIC))
    result = art.resize(CURSOR_BOX, Image.LANCZOS)
    if dim:
        result.putalpha(result.getchannel("A").point(lambda value: int(value * 0.55)))
    return result


@lru_cache(maxsize=32)
def _badge(label: str, dot: tuple, dim: bool) -> Image.Image:
    """The JUPITER status badge (so the marker is never confused with the user's own pointer); drawn once per label."""
    s = 2
    image = Image.new("RGBA", (CURSOR_BOX[0] * s, CURSOR_BOX[1] * s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    font = _font(11 * s)
    bx, by = (HOTSPOT[0] + 36 * CURSOR_SCALE) * s, (HOTSPOT[1] + 18 * CURSOR_SCALE) * s
    text_w = draw.textlength(label, font=font)
    draw.rounded_rectangle((bx, by, bx + text_w + 24 * s, by + 19 * s), radius=6 * s, fill=(3, 5, 15, 220),
                           outline=dot + (200,), width=max(1, s))
    draw.ellipse((bx + 7 * s, by + 7 * s, bx + 12 * s, by + 12 * s), fill=dot + (255,))
    draw.text((bx + 17 * s, by + 3 * s), label, font=font, fill=(226, 232, 240, 255))
    result = image.resize(CURSOR_BOX, Image.LANCZOS)
    if dim:
        result.putalpha(result.getchannel("A").point(lambda value: int(value * 0.55)))
    return result


def render_cursor(label: str, dot: tuple, *, moon_angle: float = 0.0, press: float = 0.0, dim: bool = False) -> Image.Image:
    """The Jupiter cursor at HOTSPOT inside a CURSOR_BOX RGBA image. The arrow/ring and each badge are cached; only the
    orbiting moon and the pulsing Great Red Spot vortex are drawn per frame (about a millisecond)."""
    press_bucket = max(0, min(4, round(press * 4)))
    image = _cursor_base(dim, press_bucket).copy()
    draw = ImageDraw.Draw(image)
    scale, origin, (cx, cy) = _geometry(press_bucket / 4)
    rx, ry = 22 * CURSOR_SCALE, 9 * CURSOR_SCALE
    tilt = math.radians(-15)
    mx, my = rx * math.cos(moon_angle), ry * math.sin(moon_angle)
    mx, my = cx + mx * math.cos(tilt) - my * math.sin(tilt), cy + mx * math.sin(tilt) + my * math.cos(tilt)
    fade = 140 if dim else 255
    for radius, colour in ((4 * CURSOR_SCALE, CYAN + (fade * 70 // 255,)), (2.2 * CURSOR_SCALE, WHITE + (fade,))):
        draw.ellipse((mx - radius, my - radius, mx + radius, my + radius), fill=colour)
    vx, vy = origin[0] + 13.5 * scale, origin[1] + 13.5 * scale
    pulse = 1.0 + 0.2 * math.sin(moon_angle * 2)
    k = CURSOR_SCALE
    for radius, colour in ((4.2 * k * pulse, AMBER + (fade * 80 // 255,)), (2.6 * k * pulse, RED + (fade,)),
                           (1.6 * k, AMBER + (fade,)), (0.7 * k, WHITE + (fade,))):
        draw.ellipse((vx - radius, vy - radius, vx + radius, vy + radius), fill=colour)
    return Image.alpha_composite(image, _badge(label, tuple(dot), dim))


# ── animation (deterministic: the caller supplies the clock) ─────────────────────────────────────────────────────
def ease_in_out_cubic(t: float) -> float:
    t = min(1.0, max(0.0, t))
    return 4 * t * t * t if t < 0.5 else 1 - pow(-2 * t + 2, 3) / 2


@dataclass
class Particle:
    x: float
    y: float
    vx: float
    vy: float
    colour: tuple
    size: float
    alpha: float = 1.0
    decay: float = 0.035


@dataclass
class Shockwave:
    x: float
    y: float
    started: float


@dataclass
class CursorAnimator:
    """Where the Jupiter cursor is at time ``now``: glides between action points, ripples on each action."""
    x: float
    y: float
    glide_seconds: float = 0.6
    reduced_motion: bool = False
    _from: tuple = (0.0, 0.0)
    _to: tuple = (0.0, 0.0)
    _started: float = -1.0
    particles: list = field(default_factory=list)
    waves: list = field(default_factory=list)
    _pending_wave: bool = False
    _rng: random.Random = field(default_factory=lambda: random.Random(7))

    def move_to(self, x: float, y: float, now: float, *, ripple: bool = True) -> None:
        if self.reduced_motion:
            self.x, self.y, self._started = float(x), float(y), -1.0
            if ripple:
                self.waves.append(Shockwave(self.x, self.y, now))
            return
        self._from, self._to, self._started = (self.x, self.y), (float(x), float(y)), now
        self._pending_wave = ripple

    @property
    def gliding(self) -> bool:
        return self._started >= 0

    def step(self, now: float, colour: tuple = CYAN) -> None:
        if self.gliding:
            progress = (now - self._started) / max(0.05, self.glide_seconds)
            eased = ease_in_out_cubic(progress)
            previous = (self.x, self.y)
            self.x = self._from[0] + (self._to[0] - self._from[0]) * eased
            self.y = self._from[1] + (self._to[1] - self._from[1]) * eased
            dx, dy = self.x - previous[0], self.y - previous[1]
            if not self.reduced_motion and math.hypot(dx, dy) > 1:
                for _ in range(min(4, int(math.hypot(dx, dy) / 6) + 1)):
                    self.particles.append(Particle(self.x + 8 + self._rng.uniform(-2, 2), self.y + 12 + self._rng.uniform(-2, 2),
                                                   self._rng.uniform(-0.6, 0.6) - dx * 0.15,
                                                   self._rng.uniform(-0.6, 0.6) - dy * 0.15, colour,
                                                   self._rng.uniform(1.2, 3.0)))
            if progress >= 0.999:                                       # float-safe arrival
                self.x, self.y = self._to
                self._started = -1.0
                if self._pending_wave:
                    self.waves.append(Shockwave(self.x, self.y, now))
                    if not self.reduced_motion:
                        for _ in range(16):
                            angle, speed = self._rng.uniform(0, 2 * math.pi), self._rng.uniform(1.5, 4.0)
                            self.particles.append(Particle(self.x, self.y, math.cos(angle) * speed,
                                                           math.sin(angle) * speed,
                                                           self._rng.choice((CYAN, AMBER, WHITE, ELECTRIC)),
                                                           self._rng.uniform(1.5, 3.5)))
                self._pending_wave = False
        for particle in self.particles:
            particle.x += particle.vx
            particle.y += particle.vy
            particle.alpha -= particle.decay
            particle.size *= 0.96
        self.particles = [p for p in self.particles if p.alpha > 0 and p.size > 0.2][-160:]
        self.waves = [wave for wave in self.waves if now - wave.started < 0.5]

    @property
    def busy(self) -> bool:
        return self.gliding or bool(self.particles) or bool(self.waves)


def render_effects(animator: CursorAnimator, origin: tuple, now: float) -> Image.Image:
    """Trail particles + shockwaves in a CURSOR_BOX image whose top-left is at screen ``origin``."""
    image = Image.new("RGBA", CURSOR_BOX, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    ox, oy = origin
    for particle in animator.particles:
        x, y, r = particle.x - ox, particle.y - oy, particle.size
        alpha = int(255 * max(0.0, min(1.0, particle.alpha)))
        draw.ellipse((x - r * 2, y - r * 2, x + r * 2, y + r * 2), fill=particle.colour + (alpha // 4,))
        draw.ellipse((x - r, y - r, x + r, y + r), fill=particle.colour + (alpha,))
    for wave in animator.waves:
        progress = min(1.0, (now - wave.started) / 0.5)
        radius = 2 + 68 * ease_in_out_cubic(progress)
        colour = tuple(int(c1 + (c2 - c1) * progress) for c1, c2 in zip(CYAN, AMBER))
        x, y = wave.x - ox, wave.y - oy
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), outline=colour + (int(255 * (1 - progress)),),
                     width=2)
    return image
