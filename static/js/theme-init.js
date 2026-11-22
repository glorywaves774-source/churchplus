/* Runs synchronously in <head> before the page paints.
 * - Colour theme: follows the device until the person chooses (localStorage + cookie).
 * - Sidebar collapsed state (computers and tablets).
 * - Marks installed home-screen apps so a Back button can be shown. */
(function () {
  var root = document.documentElement;
  var theme = null;
  try { theme = localStorage.getItem("cp-theme"); } catch (e) { /* storage blocked */ }
  if (!theme) {
    var m = document.cookie.match(/(?:^|; )cp_theme=(light|dark)/);
    if (m) { theme = m[1]; }
  }
  if (theme !== "light" && theme !== "dark") {
    theme = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  root.setAttribute("data-theme", theme);
  root.setAttribute("data-bs-theme", theme);

  try {
    if (localStorage.getItem("cp-sidebar") === "collapsed") { root.classList.add("sidebar-collapsed"); }
  } catch (e) { /* ignore */ }

  var standalone = (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) ||
    window.navigator.standalone === true;
  if (standalone) { root.classList.add("standalone"); }
})();
