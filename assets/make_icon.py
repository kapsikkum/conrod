r"""Draw conrod.ico from the same mark the web UI uses.

The app had no icon at all: the spec's `icon=` line is guarded by an
`.exists()` check that had always been false, so the exe wore the default
PyInstaller feather and there was nothing for a tray icon to show.

The mark is the shape of Conrod Straight, and it lives as an SVG path in
index.html. Rather than trace it by hand into a paint program, this renders
the same coordinates, so the two cannot drift apart.

Run it after changing the path:

    python assets/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

# Straight from index.html:
#   viewBox="0 0 26 72"
#   M19 3 L12 45 L13.5 50 L7.5 54 L7.5 59 L4 69
PATH = [(19, 3), (12, 45), (13.5, 50), (7.5, 54), (7.5, 59), (4, 69)]

# The UI strokes this at 3 in a 72-high box. That is a hairline once the icon
# is 16 pixels tall -- roughly a third of one pixel -- so the mark is drawn
# heavier here. Anything thinner simply is not there in the notification area.
STROKE = 7.5

ACCENT = (47, 109, 246, 255)        # --accent  #2f6df6
INK = (255, 255, 255, 255)

SUPERSAMPLE = 8
SIZES = [16, 20, 24, 32, 48, 64, 128, 256]
MARK_HEIGHT = 0.82                  # of the icon's edge
CORNER = 0.22                       # rounded-square radius, of the edge


def _draw(size: int) -> Image.Image:
    """One square icon, drawn large and reduced for smooth edges."""
    edge = size * SUPERSAMPLE
    image = Image.new("RGBA", (edge, edge), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # A filled tile rather than a bare line. On a taskbar that may be light
    # or dark, a shape with its own background is the only thing that reads
    # either way.
    draw.rounded_rectangle([0, 0, edge - 1, edge - 1],
                           radius=edge * CORNER, fill=ACCENT)

    # Fit the stroked path -- not the path -- inside the tile, so the round
    # caps do not hang over the edge.
    half = STROKE / 2
    xs = [x for x, _ in PATH]
    ys = [y for _, y in PATH]
    left, right = min(xs) - half, max(xs) + half
    top, bottom = min(ys) - half, max(ys) + half

    scale = (edge * MARK_HEIGHT) / (bottom - top)
    width = (right - left) * scale
    height = (bottom - top) * scale
    offset_x = (edge - width) / 2 - left * scale
    offset_y = (edge - height) / 2 - top * scale

    points = [(x * scale + offset_x, y * scale + offset_y) for x, y in PATH]
    thickness = max(1, round(STROKE * scale))

    # Pillow has no round join, so the corners are filled in by hand. Without
    # this the bends show a notch at the larger sizes.
    draw.line(points, fill=INK, width=thickness)
    radius = thickness / 2
    for x, y in points:
        draw.ellipse([x - radius, y - radius, x + radius, y + radius], fill=INK)

    return image.resize((size, size), Image.LANCZOS)


def main() -> None:
    here = Path(__file__).parent
    frames = [_draw(size) for size in SIZES]
    target = here / "conrod.ico"
    # Pillow writes every size it is given into the one file; Windows then
    # picks the right one for the taskbar, the tray and Explorer.
    frames[-1].save(target, format="ICO",
                    sizes=[(s, s) for s in SIZES], append_images=frames[:-1])
    print(f"wrote {target} ({target.stat().st_size:,} bytes)")

    preview = here / "icon-preview.png"
    strip = Image.new("RGBA", (sum(f.width for f in frames) + 8 * len(frames),
                               260), (20, 20, 24, 255))
    x = 4
    for frame in frames:
        strip.paste(frame, (x, (260 - frame.height) // 2), frame)
        x += frame.width + 8
    strip.save(preview)
    print(f"wrote {preview}")


if __name__ == "__main__":
    main()
