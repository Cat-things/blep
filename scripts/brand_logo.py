"""Generate the blep logo SVGs (cat-head mark + stroke-built wordmark, no font dependency).

    python scripts/brand_logo.py assets/brand
"""
import sys

PINK, RASP, GROOVE, INK, EAR = "#FF7AA2", "#D93670", "#A8234F", "#1B1B1F", "#E8548A"
TONGUE, TONGUE_EDGE = "#FF4F86", "#C2265C"

def mark(detail=True):
    # 64x64 grid. Head = ellipse + two rounded ears, all one color.
    p = []
    # one continuous silhouette: ear tips flow straight into the cheeks
    head = ("M11 5.5 L25.5 16.2 Q32 14.2 38.5 16.2 L53 5.5 "
            "C55.6 12.5 58.5 24 58.5 37 C58.5 51.5 46.5 60 32 60 "
            "C17.5 60 5.5 51.5 5.5 37 C5.5 24 8.4 12.5 11 5.5 Z")
    p.append(f'<path d="{head}" fill="{PINK}" stroke="{PINK}" stroke-width="4" stroke-linejoin="round"/>')
    p.append(f'<path d="M11.6 22.5 L13.4 12 L22.8 18.6 Z M52.4 22.5 L50.6 12 L41.2 18.6 Z" fill="{EAR}" stroke="{EAR}" stroke-width="2.4" stroke-linejoin="round"/>')
    ey, erx, ery = (35.5, 3.6, 4.4) if detail else (35, 4.6, 5.4)
    for x in (21.5, 42.5):
        p.append(f'<g class="eye"><ellipse cx="{x}" cy="{ey}" rx="{erx}" ry="{ery}" fill="{INK}"/>'
                 + (f'<circle cx="{x+1.3}" cy="{ey-1.7}" r="1.3" fill="#fff"/>' if detail else "") + '</g>')
    if detail:
        # tongue tip peeks out from under a closed cat mouth (the mouth is drawn on top of it)
        p.append(f'<g class="tongue"><path d="M28.9 45.2 H35.1 V49.6 A3.1 3.1 0 0 1 28.9 49.6 Z" fill="{TONGUE}" stroke="{TONGUE_EDGE}" stroke-width="1.1" stroke-linejoin="round"/>'
                 f'<path d="M32 47.4 V50.2" stroke="{TONGUE_EDGE}" stroke-width="1.1" stroke-linecap="round"/></g>')
        p.append(f'<path d="M30.3 41.4 H33.7 L32 43.3 Z" fill="{INK}" stroke="{INK}" stroke-width="1.3" stroke-linejoin="round"/>')
        p.append(f'<path d="M32 43.4 V44.1 C32 45.7 30.9 46.6 29.5 46.6 C28.3 46.6 27.4 45.9 27 45 M32 44.1 C32 45.7 33.1 46.6 34.5 46.6 C35.7 46.6 36.6 45.9 37 45" fill="none" stroke="{INK}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>')
    else:
        p.append(f'<path d="M24.5 43.5 C27 46 29.5 46.5 32 45.5 C34.5 46.5 37 46 39.5 43.5" fill="none" stroke="{INK}" stroke-width="2.6" stroke-linecap="round"/>')
        p.append(f'<path d="M28.6 45.6 H35.4 V50.4 A3.4 3.4 0 0 1 28.6 50.4 Z" fill="{TONGUE}" stroke="{TONGUE_EDGE}" stroke-width="1.4"/>')
    return "\n  ".join(p)

def wordmark(color, x0=0.0, w=8.0, r=12.0, gap=7.0):
    # baseline bowls centred on y=37; ascender stems from y=10, descender to y=59.5 (+cap stays inside 64)
    h = w / 2
    s = []
    bx = x0 + h                      # b stem
    s.append(f"M{bx} 10 V49 M{bx} 37 a{r} {r} 0 1 0 {2*r} 0 a{r} {r} 0 1 0 {-2*r} 0")
    lx = bx + 2*r + w + gap          # l stem
    s.append(f"M{lx} 10 V49")
    ex = lx + h + gap + h            # e left centreline
    cx = ex + r
    import math
    a = math.radians(42)
    s.append(f"M{ex} 37 H{ex+2*r} A{r} {r} 0 1 0 {cx + r*math.cos(a):.2f} {37 + r*math.sin(a):.2f}")
    px = ex + 2*r + w + gap          # p stem
    s.append(f"M{px} 25 V59.5 M{px} 37 a{r} {r} 0 1 0 {2*r} 0 a{r} {r} 0 1 0 {-2*r} 0")
    width = px + 2*r + h
    path = f'<path d="{" ".join(s)}" fill="none" stroke="{color}" stroke-width="{w}" stroke-linecap="round" stroke-linejoin="round"/>'
    return path, width

def svg(inner, vw, vh, extra=""):
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {vw} {vh}" width="{vw}" height="{vh}" fill="none"{extra}>\n  {inner}\n</svg>\n'

def lockup(text_color):
    wm, width = wordmark(text_color, x0=78)
    return svg(mark() + "\n  " + wm, round(width + 2), 64)

if __name__ == "__main__":
    out = sys.argv[1]
    open(f"{out}/mark.svg", "w").write(svg(mark(), 64, 64))
    open(f"{out}/mark-simple.svg", "w").write(svg(mark(detail=False), 64, 64))
    open(f"{out}/lockup-light.svg", "w").write(lockup(INK))
    open(f"{out}/lockup-dark.svg", "w").write(lockup("#FFFFFF"))
    wm, width = wordmark(INK)
    open(f"{out}/wordmark.svg", "w").write(svg(wm, round(width), 64))
