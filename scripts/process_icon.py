#!/usr/bin/env python3
"""Process a raw icon render into the full PWA asset set.

Usage::

    python scripts/process_icon.py SOURCE.png [--out-dir DIR]

Pipeline:
  1. Trim the white border around the rendered squircle.
  2. Flood-fill alpha-key from the corners so the rounded squircle has
     transparent corners with smooth anti-aliased edges.
  3. Pad to a square canvas.
  4. Emit PNGs at every size the PWA / browser / iOS chain needs.

Pillow is required (``pip install Pillow``).
"""

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from PIL import Image  # noqa: E402

from trellm.icon_utils import (  # noqa: E402
    alpha_key_corners,
    make_maskable,
    make_square,
    trim_to_content,
)


# Brand-fill used for maskable icons (matches the dashboard background).
BRAND_BG = (15, 17, 23, 255)

# (filename, size, "transparent" | "maskable")
OUTPUTS: list[tuple[str, int, str]] = [
    ("icon-source.png", 0, "transparent"),     # cleaned full-resolution master
    ("icon-192.png", 192, "transparent"),
    ("icon-512.png", 512, "transparent"),
    ("icon-maskable-192.png", 192, "maskable"),
    ("icon-maskable-512.png", 512, "maskable"),
    ("apple-touch-icon.png", 180, "maskable"),  # iOS prefers a solid bg
    ("favicon-32.png", 32, "transparent"),
    ("favicon-64.png", 64, "transparent"),
]


def process(source: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    raw = Image.open(source).convert("RGBA")
    trimmed = trim_to_content(raw)
    keyed = alpha_key_corners(trimmed)
    cleaned = make_square(keyed)

    for name, size, kind in OUTPUTS:
        target = out_dir / name
        if size == 0:
            cleaned.save(target, format="PNG", optimize=True)
        elif kind == "maskable":
            make_maskable(cleaned, size, BRAND_BG).save(target, format="PNG", optimize=True)
        else:
            cleaned.resize((size, size), Image.LANCZOS).save(
                target, format="PNG", optimize=True
            )
        print(f"  wrote {target.relative_to(REPO_ROOT)} ({target.stat().st_size:,} B)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Raw icon render (PNG)")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=REPO_ROOT / "trellm" / "web" / "static" / "icons",
        help="Output directory (default: trellm/web/static/icons)",
    )
    args = parser.parse_args()
    process(args.source, args.out_dir)


if __name__ == "__main__":
    main()
