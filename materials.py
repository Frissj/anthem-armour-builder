"""
Decode Anthem texture sets and bake FBX-friendly PBR maps (system Python: numpy, Pillow,
texture2ddecoder).

A texture set is {kind: Texture res} with kinds mask / normal / aomo / diffuse (catalog.py).
  mask    BC3 "source material mask": R/G/B are binary region flags; each combination is a
          paint region. The game paints regions from the player's chosen materials, which
          are not in the mesh data, so colours come from a palette here.
  aomo    R = ambient occlusion, G = cavity (edge wear)
  normal  BC5 X/Y; Z rebuilt and green flipped (DirectX -> OpenGL) for Blender
Outputs per set: <set>_basecolor.png, _roughness.png, _metallic.png, _normal.png.
"""
import os
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from texture import decode, read_header  # noqa: E402

# Frostbite format ids seen here: 0x12/0x14 RGBA8, 0x36/0x37 BC1, 0x3c BC3, 0x3f BC5
CODEC = {0x12: "rgba8", 0x14: "rgba8", 0x36: "bc1", 0x37: "bc1", 0x3C: "bc3", 0x3F: "bc5"}

# Region (R, G, B) -> (base colour sRGB, metallic, roughness). "armour" paints the armour
# pieces, "frame" the javelin's base frame (hands, joints, under-suit).
_DARK_GRACE = {
    (1, 0, 0): ((0.055, 0.055, 0.060), 0.85, 0.32),  # primary plates
    (1, 0, 1): ((0.085, 0.085, 0.090), 0.80, 0.42),  # secondary plates
    (1, 1, 0): ((0.780, 0.530, 0.130), 1.00, 0.30),  # trim / hoses
    (0, 1, 0): ((0.030, 0.030, 0.030), 0.00, 0.65),  # rubber / flex
    (0, 1, 1): ((0.550, 0.550, 0.560), 1.00, 0.30),  # bare steel details
    (0, 0, 1): ((0.780, 0.530, 0.130), 1.00, 0.28),  # accent frames
    (1, 1, 1): ((0.120, 0.120, 0.125), 0.90, 0.38),  # tertiary metal
    (0, 0, 0): ((0.040, 0.040, 0.045), 0.60, 0.50),  # none
}
_DARK_FRAME = dict(_DARK_GRACE)
_DARK_FRAME.update({
    (1, 1, 0): ((0.045, 0.045, 0.048), 0.80, 0.40),
    (0, 0, 1): ((0.070, 0.070, 0.075), 0.75, 0.45),
    (0, 1, 1): ((0.180, 0.180, 0.185), 0.90, 0.35),
})
_GUNMETAL = {
    (1, 0, 0): ((0.200, 0.205, 0.215), 0.90, 0.35),
    (1, 0, 1): ((0.320, 0.325, 0.335), 0.85, 0.40),
    (1, 1, 0): ((0.450, 0.090, 0.060), 0.30, 0.45),
    (0, 1, 0): ((0.035, 0.035, 0.035), 0.00, 0.65),
    (0, 1, 1): ((0.600, 0.600, 0.610), 1.00, 0.28),
    (0, 0, 1): ((0.750, 0.560, 0.200), 1.00, 0.30),
    (1, 1, 1): ((0.150, 0.150, 0.160), 0.90, 0.38),
    (0, 0, 0): ((0.060, 0.060, 0.065), 0.60, 0.50),
}
_IVORY = {
    (1, 0, 0): ((0.780, 0.760, 0.700), 0.10, 0.45),
    (1, 0, 1): ((0.250, 0.260, 0.280), 0.80, 0.40),
    (1, 1, 0): ((0.120, 0.300, 0.520), 0.20, 0.40),
    (0, 1, 0): ((0.035, 0.035, 0.035), 0.00, 0.65),
    (0, 1, 1): ((0.600, 0.600, 0.610), 1.00, 0.28),
    (0, 0, 1): ((0.750, 0.560, 0.200), 1.00, 0.30),
    (1, 1, 1): ((0.400, 0.400, 0.420), 0.90, 0.38),
    (0, 0, 0): ((0.080, 0.080, 0.085), 0.60, 0.50),
}
PALETTES = {
    "Dark Grace": {"armour": _DARK_GRACE, "frame": _DARK_FRAME},
    "Gunmetal": {"armour": _GUNMETAL, "frame": _DARK_FRAME},
    "Ivory": {"armour": _IVORY, "frame": _DARK_FRAME},
}


def decoded(tex_res, cache):
    """Decode a Texture res to PNG once; returns the PNG path."""
    out = os.path.join(cache, "decoded", os.path.basename(tex_res)[:-8] + ".png")
    if os.path.exists(out):
        return out
    os.makedirs(os.path.dirname(out), exist_ok=True)
    codec = CODEC[read_header(tex_res)[0]]
    img = decode(tex_res, codec)
    if codec == "bc5":
        a = np.asarray(img.convert("RGB")).astype(np.float32) / 255.0
        x, y = a[..., 0] * 2 - 1, a[..., 1] * 2 - 1
        a[..., 2] = (np.sqrt(np.clip(1 - x * x - y * y, 0, 1)) + 1) / 2
        a[..., 1] = 1.0 - a[..., 1]  # DirectX -> OpenGL
        img = Image.fromarray((a * 255 + 0.5).astype(np.uint8), "RGB")
    img.save(out)
    return out


def _load(png, size=None, mode="RGBA"):
    im = Image.open(png).convert(mode)
    if size and im.size != size:
        im = im.resize(size, Image.BILINEAR)
    return np.asarray(im).astype(np.float32) / 255.0


def _save(a, path, mode):
    Image.fromarray((np.clip(a, 0, 1) * 255 + 0.5).astype(np.uint8), mode).save(path)


def bake(name, texset, palette, frame, out_dir, cache):
    """Bake one texture set; returns {basecolor, roughness, metallic, normal} paths."""
    os.makedirs(out_dir, exist_ok=True)
    out = {k: os.path.join(out_dir, "%s_%s.png" % (name, k))
           for k in ("basecolor", "roughness", "metallic", "normal")}
    if all(os.path.exists(p) for p in out.values()):
        return out
    regions = PALETTES[palette]["frame" if frame else "armour"]
    if "mask" in texset:
        mask = _load(decoded(texset["mask"], cache))
        size = (mask.shape[1], mask.shape[0])
        r, g, b = (mask[..., i] for i in range(3))
        colour = np.zeros(mask.shape[:2] + (3,), np.float32)
        metal = np.zeros(mask.shape[:2], np.float32)
        rough = np.zeros(mask.shape[:2], np.float32)
        for (mr, mg, mb), (rgb, m, ro) in regions.items():
            w = (r if mr else 1 - r) * (g if mg else 1 - g) * (b if mb else 1 - b)
            colour += w[..., None] * np.array(rgb, np.float32)
            metal += w * m
            rough += w * ro
    elif "diffuse" in texset:
        colour = _load(decoded(texset["diffuse"], cache), mode="RGB")
        size = (colour.shape[1], colour.shape[0])
        metal = np.full(colour.shape[:2], 0.5, np.float32)
        rough = np.full(colour.shape[:2], 0.45, np.float32)
    else:
        size = (1024, 1024)
        colour = np.full((1024, 1024, 3), 0.1, np.float32)
        metal = np.full((1024, 1024), 0.7, np.float32)
        rough = np.full((1024, 1024), 0.45, np.float32)
    if "aomo" in texset:
        aomo = _load(decoded(texset["aomo"], cache), size)
        ao, cavity = aomo[..., 0], aomo[..., 1]
        # Worn edges: low cavity marks exposed edges; lighten and roughen them
        edge = np.clip((0.85 - cavity) * 2.5, 0, 1) * 0.35
        colour = colour * (1 - edge[..., None]) + np.array([0.35, 0.35, 0.36]) * edge[..., None]
        rough = np.clip(rough + edge * 0.15, 0, 1)
        colour *= (0.35 + 0.65 * ao)[..., None]
    _save(colour, out["basecolor"], "RGB")
    _save(rough, out["roughness"], "L")
    _save(metal, out["metallic"], "L")
    if "normal" in texset:
        Image.open(decoded(texset["normal"], cache)).save(out["normal"])
    else:
        _save(np.full((4, 4, 3), (0.5, 0.5, 1.0), np.float32), out["normal"], "RGB")
    return out
