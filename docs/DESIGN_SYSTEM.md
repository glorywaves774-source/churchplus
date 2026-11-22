# ChurchPlus design system

Apple-inspired premium enterprise UI: calm white surfaces, a deep navy sidebar, refined
purple and gold accents, floating glass panels (18–24px corners, soft shadows) and
restrained motion. Everything is self-hosted (Inter font, Lucide icons, Chart.js), so the
Content-Security-Policy allows only ChurchPlus's own origin.

Open **/design/** in development to see every component with sample data.

## Files

| File | Purpose |
| --- | --- |
| `static/css/core.css` | Design tokens (CSS variables), base type, public pages, print rules |
| `static/css/app.css` | App shell and all components |
| `static/css/dark.css` | Dark theme — token overrides plus a few hand-tuned surfaces |
| `static/css/mobile.css` | Phone/tablet layout, loaded last |
| `static/js/theme-init.js` | Runs in `<head>` before paint: theme, sidebar state, installed-app flag |
| `static/js/theme-toggle.js` | Dark mode / Light mode button |
| `static/js/app.js` | Shell behaviour and components (menu, dropdowns, modals, confirm, toasts, tabs, sorting) |
| `static/js/charts.js` | Chart.js wrapper that follows the theme |
| `templates/layouts/app.html` | Signed-in shell: sidebar, top bar, phone menu, tab bar |
| `templates/components/_macros.html` | Reusable Jinja components |
| `app/ui/__init__.py` | `icon()`, navigation map, `can()`, `initials` filter |
| `scripts/build_icons.py` | Rebuilds the bundled icon set from `lucide-static` |
| `scripts/ui_check.py`, `scripts/ui_interactions.py` | Phone-layout and behaviour checks |

## Rules

* **No inline scripts, styles or event handlers.** Behaviour is wired with `data-` attributes
  (`data-modal-open`, `data-confirm`, `data-dropdown-toggle`, `data-chart`, …). Tests enforce this.
* **Colours only through variables** (`var(--cp-…)`); dark mode then works automatically.
* **Typed-in data is never translated**: names, notes, the church's name and motto carry `translate="no"`.
* **Tables** sit inside `.table-wrap` so they scroll inside their own box on phones. Dates,
  codes and amounts stay on one line (`.nowrap`, `.num`); only long text wraps (`td.wrap`).
* **Forms** use 16px text so iPhones don't zoom; dates use the native date picker, which is the
  best picker on phones.
* **Destructive actions** use `ui.confirm_button(...)`, which opens the confirmation dialog.

## Phone behaviour (≤ 760px)

* ☰ sits top-left; the menu slides in from the left over a dimmed page and never pushes the page.
* The menu closes with ×, a tap on the dimmed page, Escape, or the phone's Back button.
* Choosing a page closes the menu and opens the page at the top, with no extra Back step.
* Long menus have a "Find a page" filter (accent-insensitive).
* The bottom tab bar shows 3–5 main destinations for the person's role plus Menu; places only,
  never actions. It sits above the safe area; toasts float above it.
* A Back button appears top-left only inside installed home-screen apps.
* Computers and tablets above 760px keep the normal sidebar, which can be collapsed (remembered).

## Navigation

`app/ui/__init__.py` holds the whole ChurchPlus menu. An item appears only when its page
exists **and** the person has the permission, so menus never link to unfinished or forbidden
pages. To add a module: register its blueprint with the endpoint named in `NAVIGATION`.

## Using components

```jinja
{% extends "layouts/app.html" %}
{% import "components/_macros.html" as ui with context %}
{% block title %}Members{% endblock %}
{% block content %}
  {% call ui.page_header('Members', '1,284 registered') %}
    {{ ui.button('Add member', 'user-plus', 'primary', href=url_for('members.create')) }}
  {% endcall %}
  <div class="grid grid-stats">{{ ui.stat_card('Active', '1,102', 'users') }}</div>
  {% call ui.card('All members', flush=True) %}
    {{ ui.filter_bar(url_for('members.index'), q) }}
    <div class="table-wrap"><table class="table" data-sortable>…</table></div>
    {{ ui.pagination(page, 'members.index') }}
  {% endcall %}
{% endblock %}
```

Available macros: `page_header`, `card`, `stat_card`, `button`, `confirm_button`, `badge`,
`avatar`, `person`, `profile_card`, `empty_state`, `alert`, `field` (WTForms), `input`,
`select`, `filter_bar`, `pagination`, `modal`, `dropdown`, `chart`, `feed`.

Charts: `ui.chart({"type": "line"|"bar"|"doughnut", "labels": [...], "series": [{"label": "...", "data": [...]}], "currency": "GHS", "stacked": true})`,
and include the two chart scripts in `{% block scripts %}`.

## Checking a page before release

```bash
pip install playwright && playwright install chromium
flask --app run.py run -p 8098 &
python scripts/ui_check.py http://127.0.0.1:8098 / /design/ --out shots   # 320–440px phones, tablet, desktop, both themes
python scripts/ui_interactions.py http://127.0.0.1:8098/design/
```

Chrome's phone emulation is not real Safari — always do a quick check on a real iPhone and Android phone.

## Aurora UI (1.1.0)

`static/css/aurora.css` (loaded after `dark.css`, before `mobile.css`) adds the aurora:

* `body::before` – five blurred radial lights (`--au-1` … `--au-5`), animated with transforms
  only (`aurora-drift`, 48 s); `body::after` – a faint ribbon across the top.
* Glass: `--cp-surface` is more transparent, panels get a top highlight and a hairline edge.
* Tune intensity with the `--au-*` variables (separate values for dark mode).
* Motion stops under 760 px and with `prefers-reduced-motion`; `prefers-reduced-transparency`
  makes panels solid; print hides the aurora.
