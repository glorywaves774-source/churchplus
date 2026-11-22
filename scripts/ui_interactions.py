"""Behaviour check for the app shell (development tool).

    python scripts/ui_interactions.py http://127.0.0.1:8098/design/

Checks the phone menu (open, ×, dimmed page, Escape, Android Back, choosing a
page without an extra Back step), the find-a-page filter, the desktop sidebar
collapse, dropdowns, modals, the confirmation dialog and the theme button.
"""
from __future__ import annotations

import asyncio
import os
import sys


async def main(url: str) -> int:
    from playwright.async_api import async_playwright

    fails = []

    def check(name, ok):
        print(("PASS " if ok else "FAIL ") + name)
        if not ok:
            fails.append(name)

    async with async_playwright() as p:
        exe = os.environ.get("CHROME_PATH")
        b = await (p.chromium.launch(executable_path=exe) if exe else p.chromium.launch())

        # ---------------- Phone ----------------
        ctx = await b.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        pg = await ctx.new_page()
        await pg.goto("about:blank")
        await pg.goto(url)
        is_open = lambda: pg.evaluate("document.documentElement.classList.contains('drawer-open')")

        check("menu button visible on phone", await pg.is_visible(".menu-btn"))
        check("sidebar hidden on phone", not await pg.evaluate(
            "document.getElementById('sidebar').getBoundingClientRect().right > 0"))

        await pg.click(".menu-btn"); await pg.wait_for_timeout(350)
        check("menu opens", await is_open())
        await pg.click(".sidebar-close"); await pg.wait_for_timeout(350)
        check("× closes menu", not await is_open())
        check("closing does not leave the page", pg.url == url)

        await pg.click(".menu-btn"); await pg.wait_for_timeout(350)
        await pg.mouse.click(380, 400); await pg.wait_for_timeout(350)
        check("tap on dimmed page closes menu", not await is_open())

        await pg.click(".menu-btn"); await pg.wait_for_timeout(350)
        await pg.keyboard.press("Escape"); await pg.wait_for_timeout(350)
        check("Escape closes menu", not await is_open())

        await pg.click(".menu-btn"); await pg.wait_for_timeout(350)
        await pg.go_back(); await pg.wait_for_timeout(350)
        check("Back button closes menu", not await is_open())
        check("Back button stays on page", pg.url == url)

        if await pg.query_selector("#nav-filter"):
            await pg.click(".menu-btn"); await pg.wait_for_timeout(350)
            await pg.fill("#nav-filter", "zzzz")
            check("filter hides non-matching pages", await pg.is_visible("#nav-empty"))
            await pg.fill("#nav-filter", ""); await pg.keyboard.press("Escape"); await pg.wait_for_timeout(350)

        hist_before = await pg.evaluate("history.length")
        await pg.click(".menu-btn"); await pg.wait_for_timeout(350)
        await pg.click(".sidebar .nav-link >> nth=0"); await pg.wait_for_load_state(); await pg.wait_for_timeout(500)
        check("choosing a page closes menu", not await is_open())
        check("page opens at the top", await pg.evaluate("window.scrollY") == 0)
        await pg.go_back(); await pg.wait_for_timeout(500)
        check("no extra Back step after choosing a page", pg.url in ("about:blank", url) and
              await pg.evaluate("history.length") <= hist_before + 1)
        await ctx.close()

        # ---------------- Computer ----------------
        ctx = await b.new_context(viewport={"width": 1440, "height": 900})
        pg = await ctx.new_page()
        await pg.goto(url)
        check("menu button hidden on computer", not await pg.is_visible(".menu-btn"))
        check("sidebar close hidden on computer", not await pg.is_visible(".sidebar-close"))
        await pg.click("[data-sidebar-collapse]")
        check("sidebar collapses", await pg.evaluate("document.documentElement.classList.contains('sidebar-collapsed')"))
        await pg.reload()
        check("collapsed state remembered", await pg.evaluate("document.documentElement.classList.contains('sidebar-collapsed')"))
        await pg.click("[data-sidebar-collapse]")

        await pg.click("[aria-label='Notifications']")
        check("notification centre opens", await pg.is_visible(".notif-menu"))
        await pg.keyboard.press("Escape")
        check("Escape closes dropdown", not await pg.evaluate("!!document.querySelector('.dropdown.open')"))

        if await pg.query_selector("[data-modal-open]"):
            await pg.click("[data-modal-open] >> nth=0")
            check("modal opens", await pg.evaluate("!!document.querySelector('dialog.modal[open]')"))
            await pg.keyboard.press("Escape")
            check("modal closes", not await pg.evaluate("!!document.querySelector('dialog.modal[open]')"))

        if await pg.query_selector("form[data-confirm] button"):
            await pg.click("form[data-confirm] button")
            check("confirmation dialog opens", await pg.evaluate("document.getElementById('confirm-dialog').open"))
            await pg.click("#confirm-dialog [data-modal-close]")
            check("cancel keeps the page", pg.url.startswith(url))

        theme = await pg.get_attribute("html", "data-theme")
        await pg.click("[data-theme-toggle]")
        new = await pg.get_attribute("html", "data-theme")
        await pg.reload()
        check("theme toggles and is remembered", new != theme and await pg.get_attribute("html", "data-theme") == new)
        await b.close()

    print("ALL PASSED" if not fails else f"{len(fails)} FAILED")
    return len(fails)


if __name__ == "__main__":
    raise SystemExit(1 if asyncio.run(main(sys.argv[1])) else 0)
