/* Dark mode / Light mode button. */
(function () {
  var root = document.documentElement;
  var chosen = false;
  try { chosen = !!localStorage.getItem("cp-theme"); } catch (e) { /* ignore */ }
  if (!chosen) { chosen = /(?:^|; )cp_theme=/.test(document.cookie); }

  function apply(theme, remember) {
    root.setAttribute("data-theme", theme);
    root.setAttribute("data-bs-theme", theme);
    if (remember) {
      try { localStorage.setItem("cp-theme", theme); } catch (e) { /* ignore */ }
      document.cookie = "cp_theme=" + theme + "; path=/; max-age=31536000; SameSite=Lax" +
        (location.protocol === "https:" ? "; Secure" : "");
    }
    var next = theme === "dark" ? "Light mode" : "Dark mode";
    document.querySelectorAll("[data-theme-toggle]").forEach(function (btn) {
      btn.setAttribute("aria-label", "Switch to " + next.toLowerCase());
      btn.setAttribute("title", "Switch to " + next.toLowerCase());
      var label = btn.querySelector(".theme-toggle-label");
      if (label) { label.textContent = next; }
    });
  }

  apply(root.getAttribute("data-theme") || "light", false);

  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest("[data-theme-toggle]");
    if (!btn) { return; }
    chosen = true;
    apply(root.getAttribute("data-theme") === "dark" ? "light" : "dark", true);
  });

  if (window.matchMedia) {
    var mq = window.matchMedia("(prefers-color-scheme: dark)");
    var onChange = function (e) { if (!chosen) { apply(e.matches ? "dark" : "light", false); } };
    if (mq.addEventListener) { mq.addEventListener("change", onChange); }
  }
})();
