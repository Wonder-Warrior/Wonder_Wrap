"""Generate Wonder Wrap PNG assets and multi-resolution Windows .ico."""

import math
import os
import struct
import zlib


def render_wonder_icon_rgba(w: int, h: int, rounded_bg: bool = True) -> bytes:
    """Render anti-aliased Wonder Lime (#A8FF00) on dark (#14161B) replay emblem."""
    cx, cy = w / 2.0, h / 2.0
    s = min(w, h)
    r_ring_out = s * 0.37
    r_ring_in = s * 0.275
    r_dot = s * 0.145
    corner_r = s * 0.22 if rounded_bg else 0.0

    # Supersample 2x2 for smooth anti-aliasing
    bg_r, bg_g, bg_b = 20, 22, 27          # #14161B obsidian
    lime_r, lime_g, lime_b = 168, 255, 0   # #A8FF00 Woneder Lime
    glow_r, glow_g, glow_b = 110, 175, 16  # Subtle outer lime aura

    rows = []
    for y in range(h):
        row = bytearray([0])  # PNG filter type 0
        for x in range(w):
            acc_r = acc_g = acc_b = acc_a = 0.0
            for sy in (0.25, 0.75):
                py = y + sy
                for sx in (0.25, 0.75):
                    px = x + sx
                    # Rounded square alpha mask (for .ico & square logos)
                    in_bg = True
                    if rounded_bg and w == h:
                        dx_e = max(0.0, abs(px - cx) - (w / 2.0 - corner_r))
                        dy_e = max(0.0, abs(py - cy) - (h / 2.0 - corner_r))
                        if dx_e * dx_e + dy_e * dy_e > corner_r * corner_r:
                            in_bg = False

                    if not in_bg:
                        continue

                    dx = px - cx
                    dy = py - cy
                    dist = math.hypot(dx, dy)
                    angle = math.atan2(dy, dx)  # -pi..pi

                    # Open 285-degree replay arc with crisp arrowhead gap at top-left
                    in_gap = (-2.35 < angle < -1.55)
                    on_ring = (r_ring_in <= dist <= r_ring_out) and not in_gap

                    # Arrowhead triangle at top-left tip of the replay arc
                    tip_angle = -2.35
                    ax = cx + math.cos(tip_angle) * ((r_ring_in + r_ring_out) * 0.5)
                    ay = cy + math.sin(tip_angle) * ((r_ring_in + r_ring_out) * 0.5)
                    in_arrow = math.hypot(px - ax, py - ay) <= (s * 0.095)

                    # Center Woneder Lime recording core dot
                    in_dot = dist <= r_dot

                    # Subtle radial lime glow behind ring
                    if on_ring or in_arrow or in_dot:
                        acc_r += lime_r
                        acc_g += lime_g
                        acc_b += lime_b
                        acc_a += 255.0
                    else:
                        g_factor = max(0.0, 1.0 - abs(dist - r_ring_out) / (s * 0.16)) * 0.18
                        acc_r += bg_r * (1.0 - g_factor) + glow_r * g_factor
                        acc_g += bg_g * (1.0 - g_factor) + glow_g * g_factor
                        acc_b += bg_b * (1.0 - g_factor) + glow_b * g_factor
                        acc_a += 255.0

            row.append(int(acc_r / 4.0))
            row.append(int(acc_g / 4.0))
            row.append(int(acc_b / 4.0))
            row.append(int(acc_a / 4.0))
        rows.append(bytes(row))
    return b"".join(rows)


def encode_png(w: int, h: int, raw_scanlines: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw_scanlines, 9)) + chunk(b"IEND", b"")


def render_captures_icon_rgba(w: int, h: int) -> bytes:
    """Render monochrome white (#FFFFFF) Xbox Game Bar Captures camera icon on transparent bg."""
    cx, cy = w / 2.0, h / 2.0
    s = min(w, h)
    # Camera body bounds (centered, slightly below top bump)
    bw = s * 0.62
    bh = s * 0.44
    bx0, bx1 = cx - bw / 2.0, cx + bw / 2.0
    by0, by1 = cy - bh / 2.0 + s * 0.04, cy + bh / 2.0 + s * 0.04
    cr = s * 0.07
    stroke = max(1.5, s * 0.055)

    # Top viewfinder bump
    tw = s * 0.24
    th = s * 0.08
    tx0, tx1 = cx - tw / 2.0, cx + tw / 2.0
    ty0, ty1 = by0 - th, by0 + stroke * 0.5

    # Center lens circle
    lcy = (by0 + by1) / 2.0
    lr_out = s * 0.135
    lr_in = lr_out - stroke

    def in_rounded_rect(px: float, py: float, x0: float, y0: float, x1: float, y1: float, r: float) -> bool:
        if px < x0 or px > x1 or py < y0 or py > y1:
            return False
        dx = max(0.0, max(x0 + r - px, px - (x1 - r)))
        dy = max(0.0, max(y0 + r - py, py - (y1 - r)))
        return dx * dx + dy * dy <= r * r

    rows = []
    for y in range(h):
        row = bytearray([0])
        for x in range(w):
            acc_a = 0.0
            for sy in (0.25, 0.75):
                py = y + sy
                for sx in (0.25, 0.75):
                    px = x + sx
                    on_outer = in_rounded_rect(px, py, bx0, by0, bx1, by1, cr)
                    on_inner = in_rounded_rect(
                        px, py, bx0 + stroke, by0 + stroke, bx1 - stroke, by1 - stroke, max(1.0, cr - stroke * 0.6)
                    )
                    on_body = on_outer and not on_inner
                    on_bump = in_rounded_rect(px, py, tx0, ty0, tx1, ty1, s * 0.03) and (
                        py <= ty0 + stroke or px <= tx0 + stroke or px >= tx1 - stroke
                    )
                    dist_l = math.hypot(px - cx, py - lcy)
                    on_lens = lr_in <= dist_l <= lr_out
                    if on_body or on_bump or on_lens:
                        acc_a += 255.0
            a = int(acc_a / 4.0)
            row.extend((255, 255, 255, a) if a > 0 else (0, 0, 0, 0))
        rows.append(bytes(row))
    return b"".join(rows)


def write_png(path: str, w: int, h: int, rounded_bg: bool = False, captures_mono: bool = False) -> bytes:
    rgba = render_captures_icon_rgba(w, h) if captures_mono else render_wonder_icon_rgba(w, h, rounded_bg=rounded_bg)
    png_bytes = encode_png(w, h, rgba)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(png_bytes)
    return png_bytes


def write_ico(path: str, sizes: list[int]) -> None:
    """Write a multi-resolution PNG-compressed Windows .ico file."""
    images: list[tuple[int, bytes]] = []
    for sz in sizes:
        png = encode_png(sz, sz, render_wonder_icon_rgba(sz, sz, rounded_bg=True))
        images.append((sz, png))

    # ICONDIR header: reserved(0), type(1=icon), count
    header = struct.pack("<HHH", 0, 1, len(images))
    entries = []
    offset = 6 + 16 * len(images)
    for sz, png in images:
        dim = 0 if sz >= 256 else sz
        entries.append(struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset))
        offset += len(png)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(header)
        for e in entries:
            f.write(e)
        for _, png in images:
            f.write(png)


def main() -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assets = os.path.join(root, "WonderWrapWidget", "Assets")

    for name, (w, h) in [
        ("StoreLogo.png", (50, 50)),
        ("Square44x44Logo.png", (44, 44)),
        ("Square150x150Logo.png", (150, 150)),
        ("Wide310x150Logo.png", (310, 150)),
        ("SplashScreen.png", (620, 300)),
        ("SmallTile.png", (71, 71)),
        ("LargeTile.png", (310, 310)),
    ]:
        write_png(os.path.join(assets, name), w, h, captures_mono=True)

    ico_path = os.path.join(root, "installer", "wonder_wrap.ico")
    write_ico(ico_path, [16, 24, 32, 48, 64, 128, 256])
    print(f"Generated Wonder Lime PNG assets and multi-res icon: {ico_path}")


if __name__ == "__main__":
    main()
