from __future__ import annotations

from collections.abc import Callable

PIXEL_TOPOS_ART = [
    "TTTTTTTT    OOOOOOO    PPPPPPPP     OOOOOOO      SSSSSSS",
    "   TT      OOO   OOO   PPP   PPP   OOO   OOO    SSS   SSS",
    "   TT     OOO     OOO  PPP   PPP  OOO     OOO   SSS",
    "   TT     OOO     OOO  PPPPPPPP   OOO     OOO    SSSSSS",
    "   TT     OOO     OOO  PPP        OOO     OOO         SSS",
    "   TT      OOO   OOO   PPP         OOO   OOO    SSS   SSS",
    "   TT       OOOOOOO    PPP          OOOOOOO      SSSSSSS",
]

TAGLINE = "Your Personal AI Node"


def _frame_lines(lines: list[str], *, min_width: int = 0, align: str = "left") -> list[str]:
    width = max(max((len(line) for line in lines), default=0), min_width)
    border = f"+-{'-' * width}-+"
    framed = [border]
    if align == "center":
        formatter = str.center
    else:
        formatter = str.ljust
    for line in lines:
        framed.append(f"| {formatter(line, width)} |")
    framed.append(border)
    return framed


def build_startup_banner_lines(version: str, mode: str, bind: str | None = None) -> list[str]:
    lines: list[str] = []
    lines.extend(_frame_lines(PIXEL_TOPOS_ART, min_width=72, align="left"))
    lines.extend(_frame_lines([TAGLINE], min_width=72, align="center"))
    lines.append("")
    lines.extend(_frame_lines(["Topos Node (topos-node)"], min_width=40, align="left"))
    lines.extend(_frame_lines([f"Version : v{version}"], min_width=40, align="left"))
    lines.extend(_frame_lines([f"Mode    : {mode}"], min_width=40, align="left"))
    if bind:
        lines.extend(_frame_lines([f"Bind    : {bind}"], min_width=40, align="left"))
    return lines


def emit_startup_banner(
    writer: Callable[[str], None],
    *,
    version: str,
    mode: str,
    bind: str | None = None,
) -> None:
    for line in build_startup_banner_lines(version=version, mode=mode, bind=bind):
        writer(line)
