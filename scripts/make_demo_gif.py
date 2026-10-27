"""Render docs/demo/transcript.txt into an animated terminal GIF for the README.

    pip install pillow
    python scripts/make_demo_gif.py                      # -> docs/demo/prbot-demo.gif
    python scripts/make_demo_gif.py my_run.txt out.gif   # any captured run

Transcript format: lines starting with '#' are a caption, '$ ' lines are typed out as a command,
everything else is printed as program output. Paste real output only, so the GIF stays honest.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, PAD, LINE_H, FONT_SIZE = 1100, 24, 26, 17
BG, FG, DIM, BAR = (13, 17, 23), (201, 209, 217), (110, 118, 129), (22, 27, 34)
COLORS = {
    "critical": (255, 123, 114), "high": (255, 166, 87), "medium": (210, 168, 255),
    "FAIL": (255, 123, 114), "PASS": (63, 185, 80), "patched": (63, 185, 80), "$": (121, 192, 255),
}


def _font() -> ImageFont.FreeTypeFont:
    for name in ("DejaVuSansMono.ttf", "Menlo.ttc", "/System/Library/Fonts/Menlo.ttc",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"):
        try:
            return ImageFont.truetype(name, FONT_SIZE)
        except OSError:
            continue
    return ImageFont.load_default()


def _color(line: str) -> tuple[int, int, int]:
    if line.startswith("$"):
        return COLORS["$"]
    for key in ("critical", "high", "medium", "FAIL", "PASS", "patched"):
        if key in line:
            return COLORS[key]
    return DIM if line.strip().startswith("->") else FG


def render(transcript: Path, out: Path) -> Path:
    raw = transcript.read_text().splitlines()
    caption = " ".join(l[1:].strip() for l in raw if l.startswith("#"))
    body = [l for l in raw if not l.startswith("#")]
    font = _font()
    height = PAD * 2 + 40 + LINE_H * (len(body) + 2)
    frames, durations = [], []

    def frame(lines: list[str], cursor: bool = False, hold: int = 60) -> None:
        img = Image.new("RGB", (W, height), BG)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, W, 34], fill=BAR)
        for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
            d.ellipse([16 + i * 22, 11, 28 + i * 22, 23], fill=c)
        d.text((W // 2, 17), "prbot  -  autonomous PR review + patch", font=font, fill=DIM, anchor="mm")
        y = 34 + PAD
        for ln in lines:
            d.text((PAD, y), ln, font=font, fill=_color(ln))
            y += LINE_H
        if cursor:
            x = PAD + int(d.textlength(lines[-1] if lines else "", font=font))
            d.rectangle([x + 2, y - LINE_H + 4, x + 11, y - 4], fill=FG)
        if caption:
            d.text((PAD, height - PAD - 4), caption, font=font, fill=DIM)
        frames.append(img)
        durations.append(hold)

    shown: list[str] = []
    for line in body:
        if line.startswith("$ "):
            for n in range(2, len(line) + 1, 3):
                frame(shown + [line[:n]], cursor=True, hold=35)
            shown.append(line)
            frame(shown, hold=700)
        else:
            shown.append(line)
            frame(shown, hold=1100 if ("attempt" in line or "status" in line) else 350)
    frame(shown, hold=4000)
    out.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(out, save_all=True, append_images=frames[1:], duration=durations, loop=0, optimize=True)
    return out


if __name__ == "__main__":
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("docs/demo/transcript.txt")
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("docs/demo/prbot-demo.gif")
    print(render(src, dst))
