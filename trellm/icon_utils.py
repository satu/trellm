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


WHITE_THRESHOLD = 240   # pixel is "non-content" when every RGB channel >= this
EDGE_LOW = 200          # below this, alpha stays at 255 (fully opaque)
EDGE_HIGH = 240         # at/above this, alpha goes to 0 in the flood region
FLOOD_TOLERANCE = 235   # flood-fill expands while min(r,g,b) >= this


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
    edge_low: int = EDGE_LOW,
    edge_high: int = EDGE_HIGH,
    flood_tolerance: int = FLOOD_TOLERANCE,
) -> "_Image":
    """Make corner-connected near-white background transparent.

    A 4-connected flood fill from the four corners marks the "outside" region.
    Inside that region, alpha falls off smoothly between ``edge_low`` (kept
    fully opaque) and ``edge_high`` (fully transparent), so anti-aliased
    edges around a rounded shape stay smooth instead of getting a hard ring.
    Any near-white pixels not reachable from a corner — for example, white
    highlights inside the icon — keep their original alpha.
    """
    _require_pil()
    rgba = im.convert("RGBA")
    pixels = rgba.load()
    w, h = rgba.size

    in_flood = bytearray(w * h)  # 1 byte per pixel, 0/1 flag
    stack = deque()
    for sx, sy in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        r, g, b, _ = pixels[sx, sy]
        if min(r, g, b) >= flood_tolerance:
            stack.append((sx, sy))

    while stack:
        x, y = stack.pop()
        idx = y * w + x
        if in_flood[idx]:
            continue
        r, g, b, _ = pixels[x, y]
        if min(r, g, b) < flood_tolerance:
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

    span = max(edge_high - edge_low, 1)
    out = rgba.copy()
    out_pixels = out.load()
    for y in range(h):
        row = y * w
        for x in range(w):
            if not in_flood[row + x]:
                continue
            r, g, b, a = pixels[x, y]
            mn = min(r, g, b)
            if mn >= edge_high:
                new_alpha = 0
            elif mn <= edge_low:
                new_alpha = a
            else:
                # Linear falloff: at edge_low keep full alpha, at edge_high go to 0.
                t = (mn - edge_low) / span
                new_alpha = int(round(a * (1.0 - t)))
            out_pixels[x, y] = (r, g, b, new_alpha)
    return out


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
