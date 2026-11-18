"""The "Photoshop" layer: colour grading, retouching and impact effects, all as ffmpeg filters.

What a pro editor does to every gameplay reel, and what this module adds:
  • Auto Tone     levels stretched to the clip's own shadows/highlights, dark clips lifted
  • Denoise       light temporal denoise so console-capture blocking doesn't get sharpened
  • Colour grade  a look per game (Vice neon, tactical teal-orange, stadium punch, cinematic film...)
  • Sharpen       unsharp mask on the final 1080x1920 frame (Smart Sharpen)
  • Glow          soft highlight bloom (screen-blended blur)
  • Vignette      darkened corners pull the eye to the action
  • Film grain    fine moving grain hides compression and reads "cinematic"
  • Impact FX     camera shake, white flash and RGB split on the payoff frame
  • Cut flash     a white flash on the cut from the teaser into the clip

Every filter is checked against the installed ffmpeg; anything it lacks is skipped, never fatal.
The look is chosen per clip by the AI (from the stills + trend brief), else per game from config.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import media

LOOKS: dict[str, dict] = {
    "vice": {   # GTA / Vice City: warm magenta highlights, teal shadows, neon glow
        "about": "neon Vice City grade: teal shadows, warm pink highlights, soft glow",
        "grade": ["colorbalance=rs=-0.04:gs=-0.02:bs=0.07:rm=0.03:bm=0.02:rh=0.08:gh=-0.01:bh=-0.03",
                  "eq=contrast=1.07:saturation=1.16:gamma=0.98", "vibrance=intensity=0.18"],
        "glow": 0.24, "vignette": 0.45, "grain": 4, "sharpen": 0.55,
    },
    "tactical": {   # Call of Duty: crisp teal-orange, deep blacks
        "about": "tactical teal-orange: crisp, deep blacks, punchy contrast",
        "grade": ["colorbalance=rs=-0.06:bs=0.06:rm=-0.02:bm=0.03:rh=0.06:gh=0.02:bh=-0.06",
                  "eq=contrast=1.12:saturation=1.04",
                  "curves=master='0/0 0.12/0.08 0.5/0.5 0.88/0.92 1/1'"],
        "glow": 0.0, "vignette": 0.5, "grain": 3, "sharpen": 0.85,
    },
    "stadium": {   # FC: bright broadcast pitch, saturated kits
        "about": "stadium broadcast: bright, rich greens, saturated kits",
        "grade": ["eq=contrast=1.06:saturation=1.2:gamma=1.03", "colorbalance=gm=0.03:gh=0.02",
                  "vibrance=intensity=0.15"],
        "glow": 0.10, "vignette": 0.25, "grain": 0, "sharpen": 0.65,
    },
    "cinematic": {   # film: lifted blacks, rolled-off highlights, teal-orange, grain
        "about": "cinematic film: soft contrast curve, teal-orange, grain",
        "grade": ["curves=master='0/0.04 0.25/0.22 0.75/0.8 1/0.96'",
                  "colorbalance=rs=-0.05:bs=0.05:rh=0.05:bh=-0.05", "eq=saturation=0.94:contrast=1.04"],
        "glow": 0.14, "vignette": 0.6, "grain": 7, "sharpen": 0.4,
    },
    "night": {   # dark missions: lift the shadows without greying everything
        "about": "night lift: opens up shadows, cool tint, keeps blacks",
        "grade": ["curves=master='0/0.02 0.25/0.33 0.6/0.68 1/1'", "colorbalance=bs=0.04:bm=0.02",
                  "eq=saturation=1.1:contrast=1.04"],
        "glow": 0.18, "vignette": 0.35, "grain": 3, "sharpen": 0.5,
    },
    "clean": {   # neutral: just a touch of contrast and colour
        "about": "clean: subtle contrast and colour, nothing stylised",
        "grade": ["eq=contrast=1.04:saturation=1.08"],
        "glow": 0.0, "vignette": 0.2, "grain": 0, "sharpen": 0.5,
    },
    "none": {"about": "no grading", "grade": [], "glow": 0.0, "vignette": 0.0, "grain": 0, "sharpen": 0.0},
}

GAME_LOOK = {"gta6": "vice", "cod": "tactical", "fc27": "stadium", "default": "clean"}


@dataclass
class Look:
    """Everything the editor needs to apply a look. Built by `plan()`."""
    name: str = "none"
    source_chain: list = field(default_factory=list)   # on the source frame (so the blurred fill matches)
    glow: float = 0.0
    vignette: float = 0.0
    grain: int = 0
    sharpen: float = 0.0
    shake: bool = False
    flash: bool = False
    rgb_split: bool = False
    cut_flash: bool = True
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"look": self.name, "glow": self.glow, "vignette": self.vignette, "grain": self.grain,
                "sharpen": self.sharpen, "impact": [k for k in ("shake", "flash", "rgb_split") if getattr(self, k)],
                "cut_flash": self.cut_flash, "notes": self.notes}


def _ok(filter_expr: str) -> bool:
    return media.has_filter(filter_expr.split("=", 1)[0].strip())


def _keep(chain: list[str]) -> list[str]:
    return [f for f in chain if _ok(f)]


def choose(cfg, game: str, ai_pick: str | None = None) -> str:
    """AI's pick (if valid) > config per game > built-in per game."""
    fx = (cfg.get("clips") or {}).get("fx") or {}
    fixed = fx.get("look", "auto")
    if fixed and fixed != "auto" and fixed in LOOKS:
        return fixed
    if ai_pick in LOOKS and fx.get("ai_picks_look", True):
        return ai_pick
    per_game = (cfg.game_profile(game) or {}).get("look")
    if per_game in LOOKS:
        return per_game
    return GAME_LOOK.get(game, GAME_LOOK["default"])


def auto_tone(stats: dict) -> tuple[list[str], list[str]]:
    """Photoshop-style Auto Tone from the clip's histogram. Returns (filters, notes)."""
    out, notes = [], []
    lo, hi, mean = stats.get("p2", 0.0), stats.get("p98", 255.0), stats.get("mean", 128.0)
    if (lo > 10 or hi < 238) and hi - lo > 40:
        a, b = max(0.0, lo - 2) / 255, min(255.0, hi + 4) / 255
        out.append(f"colorlevels=rimin={a:.3f}:gimin={a:.3f}:bimin={a:.3f}:rimax={b:.3f}:gimax={b:.3f}:bimax={b:.3f}")
        notes.append("auto levels")
    if mean < 62:
        out.append("eq=gamma=1.18:brightness=0.02")
        notes.append("dark clip lifted")
    elif mean > 185:
        out.append("eq=gamma=0.9")
        notes.append("bright clip pulled down")
    return out, notes


def auto_color(cs: dict) -> tuple[list[str], list[str]]:
    """Photoshop-style Auto Color for footage filmed off a screen: each channel's own black point
    (removes the grey veil and the colour cast in the shadows), one shared white point (no new casts
    in the highlights), and nothing at all if the picture is already fine."""
    if not cs:
        return [], []
    lo = {c: max(0.0, cs[c]["p1"] - 2) / 255 for c in "rgb"}
    top = max(cs[c]["p99"] for c in "rgb")
    hi = min(1.0, (top + 3) / 255) if top < 236 else 1.0
    if max(lo.values()) < 0.03 and hi >= 0.99:
        return [], []
    f = (f"colorlevels=rimin={lo['r']:.3f}:gimin={lo['g']:.3f}:bimin={lo['b']:.3f}"
         f":rimax={hi:.3f}:gimax={hi:.3f}:bimax={hi:.3f}")
    return [f], [f"auto colour (black point {int(max(lo.values()) * 255)})"]


def plan(cfg, game: str, stats: dict | None, ai_pick: str | None = None, impact: bool = True,
         graphic: bool = False, filmed: bool = False, color: dict | None = None) -> Look:
    """`filmed`: the clip was filmed off a TV with a phone. Then the job is restoring the picture
    (black level, contrast, colour, camera noise), not adding film grain or glow on top of it."""
    fx = (cfg.get("clips") or {}).get("fx") or {}
    if not fx.get("enabled", True):
        return Look(name="none", cut_flash=False)
    name = choose(cfg, game, ai_pick)
    if stats and stats.get("mean", 128) < 55 and name in ("clean", "stadium"):
        name = "night"                                  # a clean grade on a dark clip just looks muddy
    if filmed and name == "cinematic":
        name = "clean"                                  # lifted film blacks make a filmed TV look even greyer
    base = LOOKS[name]
    strength = float(fx.get("strength", 1.0))
    chain, notes = [], [base["about"]]
    if filmed and fx.get("auto_tone", True) and color:
        tone, tnotes = auto_color(color)
        chain += tone
        notes += tnotes
    elif fx.get("auto_tone", True) and stats:
        tone, tnotes = auto_tone(stats)
        chain += tone
        notes += tnotes
    if filmed:
        if media.has_filter("deflicker"):
            chain.append("deflicker=size=5:mode=pm")    # phone exposure pumping as the game's brightness changes
        chain.append("hqdn3d=3:2.5:7:7")                # phone camera noise and the TV's pixel grid
        chain.append("eq=saturation=1.06")
        notes.append("filmed off a screen: straightened, cropped, de-noised, colour restored")
    elif fx.get("denoise", True):
        chain.append("hqdn3d=1.5:1.2:5:5")
    # a filmed screen gets restored, not stylised: a teal-orange grade on top of a phone's own
    # processing pushes skin and fire to orange and clips the highlights
    chain += LOOKS["clean"]["grade"] if filmed and fx.get("look", "auto") == "auto" else base["grade"]
    look = Look(
        name=name, source_chain=_keep(chain),
        glow=0.0 if filmed else (round(base["glow"] * strength, 3) if fx.get("glow", True) else 0.0),
        vignette=round(base["vignette"] * strength * (0.5 if filmed else 1), 3) if fx.get("vignette", True) else 0.0,
        grain=0 if filmed else (int(round(base["grain"] * strength)) if fx.get("grain", True) else 0),
        sharpen=(max(0.7, base["sharpen"]) if filmed else round(base["sharpen"] * min(strength, 1.3), 3))
        if fx.get("sharpen", True) else 0.0,
        shake=bool(impact and fx.get("impact_shake", True)),
        flash=bool(impact and fx.get("impact_flash", True) and not graphic),
        rgb_split=bool(impact and fx.get("rgb_split", True)),
        cut_flash=bool(fx.get("cut_flash", True)),
        notes=notes,
    )
    return look


def apply_overrides(look: Look, o: dict) -> Look:
    """Changes the owner asked for in the chat (brighter, no shake, no grain...)."""
    for k in ("shake", "flash", "rgb_split", "cut_flash"):
        if k in o:
            setattr(look, k, bool(o[k]))
    if "grain" in o:
        look.grain = (look.grain or 4) if o["grain"] else 0
    if "glow" in o:
        look.glow = (look.glow or 0.15) if o["glow"] else 0.0
    if "vignette" in o:
        look.vignette = (look.vignette or 0.4) if o["vignette"] else 0.0
    if "sharpen" in o:
        look.sharpen = (look.sharpen or 0.6) if o["sharpen"] else 0.0
    s = o.get("look_strength")
    if s:
        look.glow, look.vignette = round(look.glow * s, 3), round(look.vignette * s, 3)
        look.grain = int(round(look.grain * s))
    if o.get("quality") == "high":                     # the owner asked for a sharper, cleaner picture
        look.sharpen = max(look.sharpen, 1.0)
        look.grain = 0
        extra = ["hqdn3d=4:3:8:8"]
        if media.has_filter("cas"):
            extra.append("cas=0.55")
        look.source_chain = list(look.source_chain) + extra
        look.notes.append("sharper and cleaner, as asked")
    b, c, sat = float(o.get("brightness", 0)), float(o.get("contrast", 0)), float(o.get("saturation", 0))
    if b or c or sat:
        look.source_chain = list(look.source_chain) + [
            f"eq=brightness={b * 0.12:.3f}:contrast={1 + c * 0.3:.3f}:saturation={1 + sat * 0.5:.3f}"]
        look.notes.append("owner's adjustments")
    return look


# ── filter-graph pieces used by editor.py ───────────────────────────────────

def canvas_chain(look: Look, inp: str, out: str, W: int, H: int, impact_at: float | None,
                 cut_flash_at: float | None) -> list[str]:
    """Graph lines that finish the 1080x1920 frame: shake, sharpen, glow, vignette, flashes, grain."""
    g, cur, n = [], inp, 0

    def step(expr: str) -> None:
        nonlocal cur, n
        n += 1
        nxt = f"{out}_{n}"
        g.append(f"[{cur}]{expr}[{nxt}]")
        cur = nxt

    P = impact_at
    if look.shake and P is not None and P > 0.2:
        D, A = 0.45, 16
        env = f"(1-(t-{P:.3f})/{D})"
        x = f"24+if(between(t,{P:.3f},{P + D:.3f}),{A}*sin(2*PI*11*(t-{P:.3f}))*{env},0)"
        y = f"24+if(between(t,{P:.3f},{P + D:.3f}),{A * 0.7:.1f}*cos(2*PI*14*(t-{P:.3f}))*{env},0)"
        step(f"crop=w={W - 48}:h={H - 48}:x='{x}':y='{y}',scale={W}:{H}:flags=bicubic,setsar=1")
    if look.sharpen > 0 and _ok("unsharp"):
        step(f"unsharp=5:5:{look.sharpen:.2f}:5:5:0")
    if look.glow > 0 and _ok("gblur") and _ok("blend"):
        n += 1
        a, b, c = f"{out}_ga{n}", f"{out}_gb{n}", f"{out}_gc{n}"
        g.append(f"[{cur}]format=yuv420p,split=2[{a}][{b}]")
        g.append(f"[{b}]scale={W // 4}:{H // 4},eq=brightness=-0.06:contrast=1.3,gblur=sigma=10,"
                 f"scale={W}:{H}:flags=bilinear[{c}]")
        nxt = f"{out}_{n}"
        g.append(f"[{a}][{c}]blend=c0_mode=screen:c0_opacity={look.glow:.2f}:"
                 f"c1_mode=normal:c1_opacity=0:c2_mode=normal:c2_opacity=0[{nxt}]")
        cur = nxt
    if look.vignette > 0 and _ok("vignette"):
        angle = 0.25 + 0.45 * min(look.vignette, 1.0)          # radians: bigger = darker corners
        step(f"vignette=angle={angle:.3f}")
    if P is not None and look.rgb_split and _ok("chromashift"):
        step(f"chromashift=cbh=-7:crh=7:enable='between(t,{P:.3f},{P + 0.2:.3f})'")
    if P is not None and look.flash and _ok("drawbox"):
        step(f"drawbox=x=0:y=0:w=iw:h=ih:color=white@0.55:t=fill:enable='between(t,{P:.3f},{P + 0.045:.3f})'")
        step(f"drawbox=x=0:y=0:w=iw:h=ih:color=white@0.22:t=fill:"
             f"enable='between(t,{P + 0.045:.3f},{P + 0.11:.3f})'")
    if cut_flash_at is not None and look.cut_flash and _ok("drawbox"):
        step(f"drawbox=x=0:y=0:w=iw:h=ih:color=white@0.7:t=fill:"
             f"enable='between(t,{cut_flash_at:.3f},{cut_flash_at + 0.05:.3f})'")
    if look.grain > 0 and _ok("noise"):
        step(f"noise=c0s={look.grain}:c0f=t+u")
    g.append(f"[{cur}]null[{out}]")
    return g
