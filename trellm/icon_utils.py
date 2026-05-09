"""Image-processing helpers for preparing PWA icon assets.

Pillow is imported lazily so the trellm runtime never pulls it in — only the
CLI script (`scripts/process_icon.py`) and the test suite touch this module.
"""

from collections import deque
from typing import TYPE_CHECKING, Tuple

if TYPE_CHECKING:
    from PIL.Image import Image as _Image


def _require_pil():
    from PIL import Image  # noqa: F401
    return Image


WHITE_THRESHOLD = 240    # pixel is "non-content" when every RGB channel >= this
DARK_THRESHOLD = 120     # min(rgb) below this = solid icon, flood stops, alpha kept
LIGHT_THRESHOLD = 180    # min(rgb) above this in flood region = alpha 0
                         # (180 sits above plausible icon body values and below
                         # the leading edge of typical drop-shadow gradients,
                         # so shadows fade to invisible)
DEFAULT_ALPHA_TRIM = 20  # ignore pixels with alpha at/below this when trimming


def trim_to_content(im: "_Image", threshold: int = WHITE_THRESHOLD) -> "_Image":
    """Crop the image to the bounding box of non-near-white pixels.

    Returns the original image if no non-white content is found (e.g. a fully
    blank input), so callers don't have to special-case empty bboxes.
    """
    Image = _require_pil()
    rgba = im.convert("RGBA")
    pixels = rgba.load()
    w, h = rgba.size

    min_x, min_y, max_x, max_y = w, h, -1, -1
    for y in range(h):
        for x in range(w):
            r, g, b, _ = pixels[x, y]
            if r < threshold or g < threshold or b < threshold:
                if x < min_x:
                    min_x = x
                if y < min_y:
                    min_y = y
                if x > max_x:
                    max_x = x
                if y > max_y:
                    max_y = y

    if max_x < 0:
        return rgba
    return rgba.crop((min_x, min_y, max_x + 1, max_y + 1))


def alpha_key_corners(
    im: "_Image",
    dark_thr: int = DARK_THRESHOLD,
    light_thr: int = LIGHT_THRESHOLD,
) -> "_Image":
    """Make the outer canvas (and any drop shadow) transparent.

    A 4-connected flood from the four corners walks every pixel where
    ``min(r, g, b) > dark_thr`` — that's the white page background, the
    soft drop shadow that fades into it, and the outermost ring of an
    anti-aliased icon edge. The flood stops as soon as it hits a pixel
    whose ``min(r, g, b) <= dark_thr`` — the icon's solid interior.

    Inside the flood region, alpha is set on a smooth ramp:

      * ``min(rgb) <= dark_thr`` → fully opaque (``alpha = 255``); these
        pixels are the boundary, the flood does not enter them.
      * ``min(rgb) >= light_thr`` → fully transparent (``alpha = 0``).
      * In between → linear fade.

    The ramp covers exactly the band where a soft drop shadow sits, so it
    fades into nothing instead of leaving a dark halo. Pixels not reached
    by the flood — either icon body or whites surrounded by darker
    content — are left untouched, so isolated highlights stay visible.
    """
    _require_pil()
    rgba = im.convert("RGBA")
    pixels = rgba.load()
    w, h = rgba.size

    in_flood = bytearray(w * h)  # 1 byte per pixel, 0/1 flag
    stack = deque()
    for sx, sy in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        r, g, b, _ = pixels[sx, sy]
        if min(r, g, b) > dark_thr:
            stack.append((sx, sy))

    while stack:
        x, y = stack.pop()
        idx = y * w + x
        if in_flood[idx]:
            continue
        r, g, b, _ = pixels[x, y]
        if min(r, g, b) <= dark_thr:
            continue
        in_flood[idx] = 1
        if x > 0:
            stack.append((x - 1, y))
        if x < w - 1:
            stack.append((x + 1, y))
        if y > 0:
            stack.append((x, y - 1))
        if y < h - 1:
            stack.append((x, y + 1))

    span = max(light_thr - dark_thr, 1)
    out = rgba.copy()
    out_pixels = out.load()
    for y in range(h):
        row = y * w
        for x in range(w):
            if not in_flood[row + x]:
                continue
            r, g, b, a = pixels[x, y]
            mn = min(r, g, b)
            if mn >= light_thr:
                new_alpha = 0
            else:
                # Linear falloff: at dark_thr keep full alpha, at light_thr go to 0.
                t = (mn - dark_thr) / span
                new_alpha = int(round(a * (1.0 - t)))
            out_pixels[x, y] = (r, g, b, new_alpha)
    return out


def trim_by_alpha(
    im: "_Image",
    alpha_thr: int = DEFAULT_ALPHA_TRIM,
) -> "_Image":
    """Crop to the bounding box of pixels whose alpha is above ``alpha_thr``.

    Use this after ``alpha_key_corners`` to shed any sub-threshold remnants
    of a faded drop shadow before squaring the icon.

    Returns the image unchanged when no pixel exceeds the threshold (e.g.
    a fully-transparent canvas), so callers don't need to special-case it.
    """
    _require_pil()
    rgba = im.convert("RGBA")
    pixels = rgba.load()
    w, h = rgba.size

    min_x, min_y, max_x, max_y = w, h, -1, -1
    for y in range(h):
        for x in range(w):
            if pixels[x, y][3] > alpha_thr:
                if x < min_x:
                    min_x = x
                if y < min_y:
                    min_y = y
                if x > max_x:
                    max_x = x
                if y > max_y:
                    max_y = y

    if max_x < 0:
        return rgba
    return rgba.crop((min_x, min_y, max_x + 1, max_y + 1))


def make_square(
    im: "_Image",
    bg_color: Tuple[int, int, int, int] = (0, 0, 0, 0),
) -> "_Image":
    """Pad the image to a square with the given background color, centred."""
    Image = _require_pil()
    rgba = im.convert("RGBA")
    w, h = rgba.size
    side = max(w, h)
    out = Image.new("RGBA", (side, side), bg_color)
    out.paste(rgba, ((side - w) // 2, (side - h) // 2), rgba)
    return out


def make_maskable(
    im: "_Image",
    size: int,
    bg_color: Tuple[int, int, int, int] = (15, 17, 23, 255),
    inner_scale: float = 1.0,
) -> "_Image":
    """Render a maskable PWA icon of `size`×`size`.

    The source is composited over a solid ``bg_color`` square of the target
    size. Any transparent corners (e.g. a squircle's rounded edges) flatten
    into the brand background, so the device's adaptive-icon mask can crop
    to any shape without revealing transparency.

    ``inner_scale`` rescales the source before compositing — pass 1.0 (the
    default) to let the design fill the canvas, or a smaller value (e.g.
    0.8) to add an explicit safe-zone margin when the source has no natural
    padding of its own.
    """
    Image = _require_pil()
    src = make_square(im.convert("RGBA"))
    inner_size = int(round(size * inner_scale))
    scaled = src.resize((inner_size, inner_size), Image.LANCZOS)
    out = Image.new("RGBA", (size, size), bg_color)
    offset = (size - inner_size) // 2
    out.paste(scaled, (offset, offset), scaled)
    return out
