"""Generate the final Balance branding assets for custom_components/.../brand/."""

import os

import cairosvg

OUT = os.path.dirname(os.path.abspath(__file__)) + "/brand"
os.makedirs(OUT, exist_ok=True)

BOLT = "M7 2v11h3v9l7-12h-4l4-8z"
AMBER = "#FFB020"
WHITE = "#FFFFFF"

MARK = f"""
  <defs><linearGradient id="plbg" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#123A5A"/><stop offset="1" stop-color="#0C6B7F"/>
  </linearGradient></defs>
  <rect width="256" height="256" rx="56" fill="url(#plbg)"/>
  <path d="M128 132 L166 210 H90 Z" fill="{WHITE}" opacity="0.95"/>
  <rect x="80" y="206" width="96" height="16" rx="8" fill="{WHITE}" opacity="0.95"/>
  <g transform="rotate(-11 128 136)">
    <rect x="30" y="124" width="196" height="22" rx="11" fill="{WHITE}"/>
  </g>
  <g transform="translate(38,34) scale(3.5)"><path d="{BOLT}" fill="{AMBER}"/></g>"""


def icon_svg():
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" '
        f'width="256" height="256">{MARK}</svg>'
    )


def logo_svg(text_fill):
    """Horizontal lockup: mark + wordmark, mark fully inside the canvas."""
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 820 220"
     width="820" height="220">
  <g transform="translate(20,20) scale(0.703)">{MARK}</g>
  <text x="230" y="127" font-family="Helvetica Neue,Helvetica,Arial,sans-serif"
        font-size="54" font-weight="600" fill="{text_fill}">Power Load Balancer</text>
</svg>"""


jobs = [
    ("icon.png", icon_svg(), 256, 256),
    ("icon@2x.png", icon_svg(), 512, 512),
    ("logo.png", logo_svg("#1B2A36"), 820, 220),
    ("logo@2x.png", logo_svg("#1B2A36"), 1640, 440),
    ("dark_logo.png", logo_svg("#FFFFFF"), 820, 220),
    ("dark_logo@2x.png", logo_svg("#FFFFFF"), 1640, 440),
]

for name, src, w, h in jobs:
    cairosvg.svg2png(
        bytestring=src.encode(), write_to=f"{OUT}/{name}", output_width=w, output_height=h
    )
    print(f"{name}: {w}x{h}")

open(f"{OUT}/../final_icon.svg", "w").write(icon_svg())
open(f"{OUT}/../final_logo.svg", "w").write(logo_svg("#1B2A36"))

# preview sheet for the logo variants
def _inner(s):
    return s[s.index(">", s.index("<svg")) + 1 : s.rindex("</svg>")]


prev = f"""<svg xmlns="http://www.w3.org/2000/svg" width="860" height="500"
     viewBox="0 0 860 500">
  <rect width="860" height="250" fill="#FFFFFF"/>
  <rect y="250" width="860" height="250" fill="#111418"/>
  <g transform="translate(20,15)">{_inner(logo_svg("#1B2A36"))}</g>
  <g transform="translate(20,265)">{_inner(logo_svg("#FFFFFF"))}</g>
</svg>"""
cairosvg.svg2png(
    bytestring=prev.encode(),
    write_to=f"{OUT}/../logo_preview.png",
    output_width=1720,
    output_height=1000,
)
print("logo preview rendered")
