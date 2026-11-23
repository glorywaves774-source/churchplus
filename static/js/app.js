/* ChurchPlus application shell and reusable components.
 * No inline scripts anywhere: every behaviour is wired through data- attributes. */
(function () {
  "use strict";
  var root = document.documentElement;
  var phoneQuery = window.matchMedia("(max-width: 760px)");

  function $(sel, ctx) { return (ctx || document).querySelector(sel); }
  function $all(sel, ctx) { return Array.prototype.slice.call((ctx || document).querySelectorAll(sel)); }

  /* ------------------------------------------------------------------ */
  /* Sidebar collapse (computers and tablets)                            */
  /* ------------------------------------------------------------------ */
  $all("[data-sidebar-collapse]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var collapsed = root.classList.toggle("sidebar-collapsed");
      btn.setAttribute("aria-expanded", String(!collapsed));
      try { localStorage.setItem("cp-sidebar", collapsed ? "collapsed" : "open"); } catch (e) { /* ignore */ }
    });
  });

  /* ------------------------------------------------------------------ */
  /* Phone menu: slides in from the left over a dimmed page.             */
  /* Closes with ×, a tap on the page, Escape or the Android Back button. */
  /* Choosing a page closes the menu without leaving an extra Back step.  */
  /* ------------------------------------------------------------------ */
  var sidebar = $("#sidebar");
  var drawerHistory = false;
  var pendingUrl = null;
  var lastFocus = null;

  function drawerOpen() { return root.classList.contains("drawer-open"); }

  function openDrawer() {
    if (!sidebar || drawerOpen()) { return; }
    lastFocus = document.activeElement;
    root.classList.add("drawer-open");
    sidebar.setAttribute("aria-hidden", "false");
    $all("[data-drawer-open]").forEach(function (b) { b.setAttribute("aria-expanded", "true"); });
    try { history.pushState({ cpDrawer: true }, ""); drawerHistory = true; } catch (e) { drawerHistory = false; }
    var filter = $("#nav-filter");
    var first = filter || $(".nav-link", sidebar);
    if (first && !filter) { first.focus({ preventScroll: true }); }
    else { var close = $("[data-drawer-close]", sidebar); if (close) { close.focus({ preventScroll: true }); } }
  }

  function hideDrawer() {
    root.classList.remove("drawer-open");
    if (sidebar && phoneQuery.matches) { sidebar.setAttribute("aria-hidden", "true"); }
    $all("[data-drawer-open]").forEach(function (b) { b.setAttribute("aria-expanded", "false"); });
    if (lastFocus && lastFocus.focus) { lastFocus.focus({ preventScroll: true }); }
  }

  function closeDrawer() {
    if (!drawerOpen()) { return; }
    if (drawerHistory) { drawerHistory = false; history.back(); }  // popstate hides it
    else { hideDrawer(); }
  }

  window.addEventListener("popstate", function () {
    if (drawerOpen()) { drawerHistory = false; hideDrawer(); }
    if (pendingUrl) { var url = pendingUrl; pendingUrl = null; window.location.assign(url); }
  });

  $all("[data-drawer-open]").forEach(function (b) { b.addEventListener("click", openDrawer); });
  $all("[data-drawer-close]").forEach(function (b) { b.addEventListener("click", closeDrawer); });

  if (sidebar) {
    sidebar.addEventListener("click", function (ev) {
      var link = ev.target.closest("a.nav-link");
      if (!link || !drawerOpen() || !drawerHistory) { return; }
      if (ev.metaKey || ev.ctrlKey || ev.shiftKey || link.target === "_blank") { return; }
      ev.preventDefault();
      pendingUrl = link.href;
      drawerHistory = false;
      history.back();                       // remove the menu's history entry first
      setTimeout(function () {             // fallback if popstate does not fire
        if (pendingUrl) { var u = pendingUrl; pendingUrl = null; window.location.assign(u); }
      }, 400);
    });
  }

  function syncSidebarAria() {
    if (!sidebar) { return; }
    if (phoneQuery.matches) { sidebar.setAttribute("aria-hidden", drawerOpen() ? "false" : "true"); }
    else { sidebar.removeAttribute("aria-hidden"); root.classList.remove("drawer-open"); }
  }
  syncSidebarAria();
  if (phoneQuery.addEventListener) { phoneQuery.addEventListener("change", syncSidebarAria); }

  /* Find a page: accent-insensitive filter at the top of the menu. */
  function fold(s) { return (s || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase(); }
  var navFilter = $("#nav-filter");
  if (navFilter) {
    navFilter.addEventListener("input", function () {
      var q = fold(navFilter.value.trim());
      var any = false;
      $all(".nav-section", sidebar).forEach(function (section) {
        var visible = 0;
        $all(".nav-link", section).forEach(function (link) {
          var show = !q || fold(link.textContent).indexOf(q) !== -1;
          link.hidden = !show;
          if (show) { visible += 1; }
        });
        section.hidden = visible === 0;
        if (visible) { any = true; }
      });
      var empty = $("#nav-empty");
      if (empty) { empty.hidden = any; }
    });
  }

  /* ------------------------------------------------------------------ */
  /* Dropdowns                                                           */
  /* ------------------------------------------------------------------ */
  /* Cards are separate layers (glass blur), so a later card would paint over an open menu.
     While a menu is open, lift every box around it above its neighbours. */
  function liftAncestors(dd, on) {
    for (var el = dd.parentElement; el && el !== document.body; el = el.parentElement) {
      if (on) {
        el.classList.add(getComputedStyle(el).position === "static" ? "menu-host-static" : "menu-host");
      } else {
        el.classList.remove("menu-host", "menu-host-static");
      }
      if (el.classList.contains("app-main") || el.classList.contains("topbar")) { break; }
    }
  }
  function closeDropdowns(except) {
    $all(".dropdown.open").forEach(function (d) {
      if (d !== except) {
        d.classList.remove("open");
        liftAncestors(d, false);
        var t = $("[data-dropdown-toggle]", d);
        if (t) { t.setAttribute("aria-expanded", "false"); }
      }
    });
  }
  document.addEventListener("click", function (ev) {
    var toggle = ev.target.closest("[data-dropdown-toggle]");
    if (toggle) {
      var dd = toggle.closest(".dropdown");
      var open = !dd.classList.contains("open");
      closeDropdowns(dd);
      dd.classList.toggle("open", open);
      liftAncestors(dd, open);
      toggle.setAttribute("aria-expanded", String(open));
      if (open) { var first = $(".dropdown-item", dd); if (first) { first.focus({ preventScroll: true }); } }
      return;
    }
    if (!ev.target.closest(".dropdown-menu")) { closeDropdowns(null); }
  });

  /* ------------------------------------------------------------------ */
  /* Modals (native <dialog>)                                            */
  /* ------------------------------------------------------------------ */
  document.addEventListener("click", function (ev) {
    var opener = ev.target.closest("[data-modal-open]");
    if (opener) {
      var dlg = document.getElementById(opener.getAttribute("data-modal-open"));
      if (dlg && dlg.showModal) { ev.preventDefault(); dlg.showModal(); }
      return;
    }
    var closer = ev.target.closest("[data-modal-close]");
    if (closer) { var d = closer.closest("dialog"); if (d) { d.close(); } return; }
    if (ev.target.tagName === "DIALOG" && ev.target.classList.contains("modal")) {
      var r = ev.target.getBoundingClientRect();           // click on the backdrop closes
      if (ev.clientX < r.left || ev.clientX > r.right || ev.clientY < r.top || ev.clientY > r.bottom) { ev.target.close(); }
    }
  });

  /* Confirmation dialog for destructive actions:
   * <form data-confirm="Delete this member?" data-confirm-action="Delete"> */
  var confirmDlg = $("#confirm-dialog");
  var pendingForm = null;
  document.addEventListener("submit", function (ev) {
    var form = ev.target;
    if (!form.hasAttribute("data-confirm") || form.dataset.confirmed === "1" || !confirmDlg) { return; }
    ev.preventDefault();
    pendingForm = form;
    $("#confirm-title", confirmDlg).textContent = form.getAttribute("data-confirm-title") || "Are you sure?";
    $("#confirm-message", confirmDlg).textContent = form.getAttribute("data-confirm");
    $("#confirm-ok", confirmDlg).textContent = form.getAttribute("data-confirm-action") || "Confirm";
    confirmDlg.showModal();
  });
  if (confirmDlg) {
    $("#confirm-ok", confirmDlg).addEventListener("click", function () {
      confirmDlg.close();
      if (pendingForm) {
        pendingForm.dataset.confirmed = "1";
        if (pendingForm.requestSubmit) { pendingForm.requestSubmit(); } else { pendingForm.submit(); }
        pendingForm = null;
      }
    });
  }

  /* ------------------------------------------------------------------ */
  /* Escape key: closes dropdowns, then the phone menu.                  */
  /* ------------------------------------------------------------------ */
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape") {
      if ($(".dropdown.open")) { closeDropdowns(null); return; }
      if (drawerOpen()) { closeDrawer(); }
    }
    if ((ev.key === "k" || ev.key === "K") && (ev.metaKey || ev.ctrlKey)) {
      var s = $("#global-search-input");
      if (s) { ev.preventDefault(); s.focus(); s.select(); }
    }
  });

  /* ------------------------------------------------------------------ */
  /* Toasts: server flash messages arrive as <div data-toast="...">      */
  /* ------------------------------------------------------------------ */
  var ICONS = { success: "check-circle", danger: "alert", error: "alert", warning: "alert", info: "info" };
  function toast(message, kind) {
    var stack = $("#toast-stack");
    if (!stack) { return; }
    var el = document.createElement("div");
    el.className = "toast toast-" + (kind || "info");
    el.setAttribute("role", kind === "danger" || kind === "error" ? "alert" : "status");
    var tpl = $("#toast-icon-" + (ICONS[kind] ? kind : "info"));
    var icon = document.createElement("span");
    icon.className = "toast-icon";
    if (tpl) { icon.innerHTML = tpl.innerHTML; }
    var msg = document.createElement("div");
    msg.className = "toast-msg";
    msg.textContent = message;
    var close = document.createElement("button");
    close.type = "button";
    close.className = "btn btn-ghost btn-icon btn-sm";
    close.setAttribute("aria-label", "Dismiss");
    close.textContent = "×";
    el.appendChild(icon); el.appendChild(msg); el.appendChild(close);
    stack.appendChild(el);
    function dismiss() { el.classList.add("leaving"); setTimeout(function () { el.remove(); }, 260); }
    close.addEventListener("click", dismiss);
    if (kind !== "danger" && kind !== "error") { setTimeout(dismiss, 5000); }
  }
  window.ChurchPlus = window.ChurchPlus || {};
  window.ChurchPlus.toast = toast;
  $all("[data-toast]").forEach(function (el) { toast(el.getAttribute("data-toast"), el.getAttribute("data-kind")); el.remove(); });
  $all("[data-demo-toast]").forEach(function (b) {
    b.addEventListener("click", function () { toast(b.getAttribute("data-demo-toast"), b.getAttribute("data-kind")); });
  });

  /* ------------------------------------------------------------------ */
  /* Tabs                                                                */
  /* ------------------------------------------------------------------ */
  $all("[role=tablist]").forEach(function (list) {
    list.addEventListener("click", function (ev) {
      var tab = ev.target.closest("[role=tab]");
      if (!tab) { return; }
      $all("[role=tab]", list).forEach(function (t) {
        var on = t === tab;
        t.setAttribute("aria-selected", String(on));
        var panel = document.getElementById(t.getAttribute("aria-controls"));
        if (panel) { panel.hidden = !on; }
      });
    });
  });

  /* Open the tab that contains the element named in the address (#documents, #family …) */
  function showHashTarget() {
    if (!location.hash || location.hash.length < 2) { return; }
    var el = document.getElementById(decodeURIComponent(location.hash.slice(1)));
    var panel = el && el.closest("[role=tabpanel]");
    if (panel && panel.hidden) {
      var tab = document.querySelector('[role=tab][aria-controls="' + panel.id + '"]');
      if (tab) { tab.click(); }
    }
    if (el) { el.scrollIntoView({ block: "start" }); }
  }
  showHashTarget();
  window.addEventListener("hashchange", showHashTarget);

  /* Filter chips toggle */
  document.addEventListener("click", function (ev) {
    var chip = ev.target.closest(".chip[aria-pressed]");
    if (chip) { chip.setAttribute("aria-pressed", chip.getAttribute("aria-pressed") === "true" ? "false" : "true"); }
  });

  /* ------------------------------------------------------------------ */
  /* Client-side table sorting: <th data-sort="text|number|date">        */
  /* ------------------------------------------------------------------ */
  $all("table[data-sortable]").forEach(function (table) {
    $all("th[data-sort]", table).forEach(function (th, idx) {
      th.setAttribute("tabindex", "0");
      function sort() {
        var type = th.getAttribute("data-sort");
        var dir = th.getAttribute("aria-sort") === "ascending" ? "descending" : "ascending";
        $all("th[data-sort]", table).forEach(function (o) { o.removeAttribute("aria-sort"); });
        th.setAttribute("aria-sort", dir);
        var col = Array.prototype.indexOf.call(th.parentNode.children, th);
        var body = table.tBodies[0];
        var rows = Array.prototype.slice.call(body.rows);
        rows.sort(function (a, b) {
          var x = a.cells[col].getAttribute("data-value") || a.cells[col].textContent.trim();
          var y = b.cells[col].getAttribute("data-value") || b.cells[col].textContent.trim();
          if (type === "number") { x = parseFloat(x.replace(/[^0-9.-]/g, "")) || 0; y = parseFloat(y.replace(/[^0-9.-]/g, "")) || 0; }
          var r = x > y ? 1 : x < y ? -1 : 0;
          return dir === "ascending" ? r : -r;
        });
        rows.forEach(function (r) { body.appendChild(r); });
      }
      th.addEventListener("click", sort);
      th.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); sort(); } });
    });
  });

  /* Print buttons */
  $all("[data-print]").forEach(function (b) { b.addEventListener("click", function () { window.print(); }); });

  /* Back button (installed home-screen apps only) */
  $all("[data-app-back]").forEach(function (b) { b.addEventListener("click", function () { history.back(); }); });

  /* Show parts of a form only when another field has a value: data-show-when="name=value[,value]" */
  $all("[data-show-when]").forEach(function (el) {
    var spec = el.getAttribute("data-show-when").split("=");
    var form = el.closest("form") || document;
    var values = spec[1].split(",");
    function update() {
      var field = form.querySelector('[name="' + spec[0] + '"]:checked') || form.querySelector('select[name="' + spec[0] + '"]');
      var on = field && values.indexOf(field.value) !== -1;
      el.hidden = !on;
      el.querySelectorAll("input, select, textarea").forEach(function (i) { i.disabled = !on; });
    }
    form.querySelectorAll('[name="' + spec[0] + '"]').forEach(function (f) { f.addEventListener("change", update); });
    update();
  });

  /* Live character / SMS part counter: <textarea data-count="#target"> */
  $all("[data-count]").forEach(function (ta) {
    var out = document.querySelector(ta.getAttribute("data-count"));
    if (!out) { return; }
    var gsm = /^[@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !"#¤%&'()*+,\-./0-9:;<=>?¡A-ZÄÖÑÜ§¿a-zäöñüà^{}\\\[~\]|€]*$/;
    function update() {
      var t = ta.value, n = t.length, isGsm = gsm.test(t);
      var single = isGsm ? 160 : 70, multi = isGsm ? 153 : 67;
      var parts = n === 0 ? 0 : (n <= single ? 1 : Math.ceil(n / multi));
      out.textContent = n + " characters · " + parts + " SMS part" + (parts === 1 ? "" : "s") + (isGsm ? "" : " (special characters)");
    }
    ta.addEventListener("input", update);
    update();
  });

  /* Installable app: service worker + install button */
  if ("serviceWorker" in navigator && document.documentElement.getAttribute("data-sw")) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register(document.documentElement.getAttribute("data-sw"), { scope: "/" }).catch(function () {});
    });
  }
  var deferredInstall = null;
  var installButtons = $all("[data-install-app]");
  var isStandalone = (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) || window.navigator.standalone === true;
  var isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  function showInstall(on) { installButtons.forEach(function (b) { b.hidden = !on; }); }
  showInstall(!isStandalone && isIOS);
  window.addEventListener("beforeinstallprompt", function (e) {
    e.preventDefault();
    deferredInstall = e;
    showInstall(!isStandalone);
  });
  window.addEventListener("appinstalled", function () { deferredInstall = null; showInstall(false); });
  installButtons.forEach(function (b) {
    b.addEventListener("click", function () {
      if (deferredInstall) {
        deferredInstall.prompt();
        deferredInstall.userChoice.then(function () { deferredInstall = null; showInstall(false); });
      } else {
        var help = document.getElementById("install-help");
        if (help && help.showModal) { help.showModal(); }
      }
    });
  });
})();

