"""Tests for icon_utils image-processing helpers."""

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from trellm import icon_utils  # noqa: E402


def _solid(size, color):
    return Image.new("RGBA", size, color)


def test_trim_to_content_crops_white_border():
    im = _solid((20, 20), (255, 255, 255, 255))
    # Place a 5x5 black square at (7,7)
    for y in range(7, 12):
        for x in range(7, 12):
            im.putpixel((x, y), (0, 0, 0, 255))

    cropped = icon_utils.trim_to_content(im)

    assert cropped.size == (5, 5)
    assert cropped.getpixel((0, 0)) == (0, 0, 0, 255)


def test_trim_to_content_no_crop_when_already_tight():
    im = _solid((4, 4), (10, 20, 30, 255))
    cropped = icon_utils.trim_to_content(im)
    assert cropped.size == (4, 4)


def test_trim_to_content_passes_through_when_fully_white():
    """If everything is white, trim returns the original (avoid empty bbox)."""
    im = _solid((10, 10), (255, 255, 255, 255))
    out = icon_utils.trim_to_content(im)
    assert out.size == (10, 10)


def test_alpha_key_makes_corner_whites_transparent():
    im = _solid((10, 10), (255, 255, 255, 255))
    # Dark 4x4 block in the centre
    for y in range(3, 7):
        for x in range(3, 7):
            im.putpixel((x, y), (10, 20, 30, 255))

    out = icon_utils.alpha_key_corners(im)

    # Corner is now transparent
    assert out.getpixel((0, 0))[3] == 0
    assert out.getpixel((9, 9))[3] == 0
    # Centre dark pixel is opaque, colour preserved
    cx = out.getpixel((5, 5))
    assert cx[3] == 255
    assert cx[:3] == (10, 20, 30)


def test_alpha_key_preserves_interior_whites():
    """White pixels not reachable from any corner must stay opaque."""
    im = _solid((20, 20), (255, 255, 255, 255))
    # Dark ring fully enclosing the centre (single-pixel-wide is enough since
    # flood-fill is 4-connected)
    for y in range(5, 15):
        for x in range(5, 15):
            on_ring = y in (5, 14) or x in (5, 14)
            if on_ring:
                im.putpixel((x, y), (10, 10, 10, 255))
    # Interior white at (10, 10) — surrounded by dark ring, not reachable
    # from corners.
    assert im.getpixel((10, 10)) == (255, 255, 255, 255)

    out = icon_utils.alpha_key_corners(im)

    # Outer corner: transparent
    assert out.getpixel((0, 0))[3] == 0
    # Dark ring: opaque
    assert out.getpixel((5, 10))[3] == 255
    # Interior white: still opaque (flood didn't reach it)
    assert out.getpixel((10, 10))[3] == 255


def test_alpha_key_smooth_falloff_at_edge():
    """Pixels in the fade zone get partial alpha, monotonically by lightness."""
    # A 1-row gradient: from pure white -> mid-grey -> dark
    im = Image.new("RGBA", (5, 1), (255, 255, 255, 255))
    im.putpixel((0, 0), (255, 255, 255, 255))  # white  -> alpha 0
    im.putpixel((1, 0), (240, 240, 240, 255))  # nearly white -> low alpha
    im.putpixel((2, 0), (220, 220, 220, 255))  # mid -> partial
    im.putpixel((3, 0), (200, 200, 200, 255))  # darker -> alpha 255 (boundary)
    im.putpixel((4, 0), (50, 50, 50, 255))     # dark -> alpha 255

    out = icon_utils.alpha_key_corners(im)
    alphas = [out.getpixel((x, 0))[3] for x in range(5)]
    # Pure white connected from corner → fully transparent
    assert alphas[0] == 0
    # Dark pixels are fully opaque
    assert alphas[4] == 255
    # Monotonic (non-decreasing) from light to dark
    for a, b in zip(alphas, alphas[1:]):
        assert a <= b, f"alpha not monotonic: {alphas}"


def test_make_square_pads_with_transparent():
    im = _solid((10, 20), (10, 10, 10, 255))
    out = icon_utils.make_square(im)
    assert out.size == (20, 20)
    # Original content is centred horizontally → x=0..4 is padding
    assert out.getpixel((0, 0)) == (0, 0, 0, 0)
    assert out.getpixel((10, 10)) == (10, 10, 10, 255)


def test_make_maskable_default_fills_canvas():
    """Default inner_scale=1.0 lets the design fill the maskable canvas."""
    src = _solid((100, 100), (50, 100, 200, 255))
    out = icon_utils.make_maskable(src, size=200, bg_color=(15, 17, 23, 255))

    assert out.size == (200, 200)
    # Edge and centre are both the source colour (canvas fully covered).
    assert out.getpixel((0, 0)) == (50, 100, 200, 255)
    assert out.getpixel((100, 100)) == (50, 100, 200, 255)


def test_make_maskable_inner_scale_adds_safe_zone():
    """An explicit inner_scale leaves a brand-bg margin around the design."""
    src = _solid((100, 100), (50, 100, 200, 255))

    out = icon_utils.make_maskable(
        src, size=200, bg_color=(15, 17, 23, 255), inner_scale=0.8
    )

    assert out.size == (200, 200)
    # Outside the inner zone (e.g. the corner) should be the background.
    assert out.getpixel((2, 2)) == (15, 17, 23, 255)
    # Centre should still be the source colour.
    assert out.getpixel((100, 100)) == (50, 100, 200, 255)
    # Inner bound is 200 * (1-0.8)/2 = 20px from each edge.
    assert out.getpixel((20, 100))[:3] == (50, 100, 200)
    assert out.getpixel((19, 100))[:3] == (15, 17, 23)


def test_make_maskable_flattens_transparent_corners_into_bg():
    """A transparent-corner source is filled with bg_color in the corners."""
    src = _solid((100, 100), (50, 100, 200, 255))
    # Punch a transparent corner.
    for y in range(0, 5):
        for x in range(0, 5):
            src.putpixel((x, y), (0, 0, 0, 0))

    out = icon_utils.make_maskable(src, size=100, bg_color=(15, 17, 23, 255))

    assert out.getpixel((0, 0)) == (15, 17, 23, 255)
    assert out.getpixel((50, 50)) == (50, 100, 200, 255)
