"""Visual and phone-layout check (development tool).

Crawls the given pages at phone, tablet and computer sizes in light and dark
mode, then reports sideways scrolling, console/CSP errors and saves screenshots.

    pip install playwright && playwright install chromium
    flask --app run.py run -p 8098 &
    python scripts/ui_check.py http://127.0.0.1:8098 / /design/ --out shots

Chrome's phone emulation is not real Safari: always do a quick check on a real
iPhone and Android phone before release.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

SIZES = {
    "phone-320": (320, 640),
    "android-360": (360, 780),
    "phone-390": (390, 844),
    "iphone18pro-402": (402, 734),       # ~734pt visible in Safari
    "iphone18promax-440": (440, 956),
    "tablet-820": (820, 1180),
    "desktop-1440": (1440, 900),
}


async def _login(page, base, email, password):
    await page.goto(base + "/login")
    await page.fill("input[name=email]", email)
    await page.fill("input[name=password]", password)
    await page.click("button[type=submit]")
    await page.wait_for_load_state("networkidle")


async def run(base: str, paths: list[str], out: Path, full: bool, executable: str | None,
              login: tuple[str, str] | None = None) -> int:
    from playwright.async_api import async_playwright

    out.mkdir(parents=True, exist_ok=True)
    problems = 0
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=executable) if executable else await p.chromium.launch()
        state = None
        if login:                      # sign in once and reuse the session (sign-in is rate limited)
            c = await browser.new_context()
            pg = await c.new_page()
            await _login(pg, base, *login)
            state = await c.storage_state()
            await c.close()
        for theme in ("light", "dark"):
            for size, (w, h) in SIZES.items():
                ctx = await browser.new_context(viewport={"width": w, "height": h}, color_scheme=theme,
                                                is_mobile=w < 761, has_touch=w < 761, storage_state=state)
                page = await ctx.new_page()
                errors: list[str] = []
                page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
                page.on("pageerror", lambda e: errors.append(str(e)))
                for path in paths:
                    errors.clear()
                    await page.goto(base + path, wait_until="networkidle")
                    await page.wait_for_timeout(300)
                    sw = await page.evaluate("document.documentElement.scrollWidth")
                    if sw > w:
                        problems += 1
                        wide = await page.evaluate(
                            """(w) => Array.from(document.querySelectorAll('body *')).filter(e => {
                                 const r = e.getBoundingClientRect(); return r.right > w + 1 && !e.closest('.table-wrap, .sidebar, .tabs');
                               }).slice(0, 5).map(e => e.tagName + '.' + e.className)""", w)
                        print(f"SIDEWAYS SCROLL {theme} {size} {path}: {sw}px > {w}px {wide}")
                    # Cards or panels cut off at the right edge (hidden overflow hides them from scrollWidth).
                    clipped = await page.evaluate(
                        """(w) => Array.from(document.querySelectorAll('main .card, main .stat-card, main form, main .alert'))
                             .filter(e => { const r = e.getBoundingClientRect(); return r.width && r.right > w + 1 && !e.closest('.table-wrap, .sidebar, .tabs, dialog'); })
                             .slice(0, 3).map(e => e.tagName + '.' + e.className)""", w)
                    if clipped:
                        problems += 1
                        print(f"CLIPPED {theme} {size} {path}: {clipped}")
                    real = [e for e in errors if "404" not in e]
                    if real:
                        problems += 1
                        print(f"CONSOLE {theme} {size} {path}: {real[:3]}")
                    name = f"{theme}_{size}_{path.strip('/').replace('/', '_') or 'home'}.png"
                    await page.screenshot(path=str(out / name), full_page=full)
                await ctx.close()
        await browser.close()
    print("OK" if not problems else f"{problems} problem(s)")
    return problems


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("base")
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--out", default="shots")
    ap.add_argument("--full", action="store_true", help="full-page screenshots")
    ap.add_argument("--chrome", default=os.environ.get("CHROME_PATH"))
    ap.add_argument("--login", nargs=2, metavar=("EMAIL", "PASSWORD"), help="sign in before checking")
    a = ap.parse_args()
    raise SystemExit(1 if asyncio.run(run(a.base, a.paths, Path(a.out), a.full, a.chrome, a.login)) else 0)


if __name__ == "__main__":
    main()
