"""Build app/ui/icons.json from the lucide-static package.

Usage:
    npm pack lucide-static && tar xzf lucide-static-*.tgz
    python scripts/build_icons.py package/icons

Only the icons listed in ICONS are bundled, so pages stay light. Add a name
here and re-run to make a new icon available to the ``icon()`` macro.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ICONS = """
activity alarm-clock alert-circle alert-triangle archive arrow-down arrow-left arrow-right arrow-up
arrow-up-right arrow-down-right badge-check banknote bar-chart-3 bell bell-ring book book-open
bookmark briefcase building building-2 cake calendar calendar-check calendar-days calendar-plus
camera check check-circle-2 chevron-down chevron-left chevron-right chevron-up chevrons-left
chevrons-right church circle-dollar-sign clipboard-check clipboard-list clock cloud-upload
coins copy credit-card download edit eye eye-off file file-text files filter folder gift
graduation-cap hand-heart hand-coins handshake heart heart-handshake help-circle history home
id-card image inbox info landmark layers layout-dashboard library line-chart link list
list-checks loader-2 lock log-in log-out mail map-pin megaphone menu message-circle
message-square mic minus monitor moon more-horizontal more-vertical music network
panel-left-close panel-left-open paperclip pen-line phone pie-chart piggy-bank play plus
plus-circle printer qr-code receipt refresh-cw repeat save scale school search send settings
settings-2 share-2 shield shield-alert shield-check sliders-horizontal smartphone sparkles
star sun table tag target trash-2 trending-down trending-up upload user user-check user-cog
user-minus user-plus user-round user-x users users-round video wallet x x-circle zap
pin globe file-down file-spreadsheet wifi-off external-link server database key-round share plus-square download-cloud cookie badge-percent
""".split()

ALIASES = {
    # Lucide renamed some icons; keep friendly names working.
    "alert-circle": "circle-alert", "alert-triangle": "triangle-alert", "bar-chart-3": "chart-column",
    "check-circle-2": "circle-check", "edit": "square-pen", "help-circle": "circle-question-mark",
    "home": "house", "line-chart": "chart-line", "loader-2": "loader-circle", "pie-chart": "chart-pie",
    "plus-circle": "circle-plus", "x-circle": "circle-x", "more-horizontal": "ellipsis",
    "more-vertical": "ellipsis-vertical", "settings-2": "settings-2", "sliders-horizontal": "sliders-horizontal",
    "circle-dollar-sign": "circle-dollar-sign", "help": "circle-question-mark",
}


def inner_svg(path: Path) -> str:
    text = path.read_text()
    body = re.search(r"<svg[^>]*>(.*)</svg>", text, re.S).group(1)
    return re.sub(r"\s+", " ", body).strip()


def main(src: str) -> None:
    src_dir = Path(src)
    out: dict[str, str] = {}
    missing = []
    for name in ICONS:
        candidates = [name, ALIASES.get(name, name)]
        for cand in candidates:
            f = src_dir / f"{cand}.svg"
            if f.exists():
                out[name] = inner_svg(f)
                break
        else:
            missing.append(name)
    dest = Path(__file__).resolve().parent.parent / "app" / "ui" / "icons.json"
    dest.write_text(json.dumps(out, sort_keys=True, indent=0))
    print(f"Wrote {len(out)} icons to {dest}")
    if missing:
        print("Missing:", ", ".join(missing))
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "package/icons")
