/* Attendance check-in: submit without reloading, keep the cursor in the box (works with
 * USB/Bluetooth card scanners), and scan QR codes with the camera where the browser
 * supports the BarcodeDetector API (Chrome on Android, Edge, Chrome on desktop). */
(function () {
  "use strict";
  var form = document.querySelector("form[data-checkin]");
  if (!form) { return; }
  var input = form.querySelector("[name=code]");
  var busy = false;

  function submit(code) {
    if (busy || !code) { return; }
    busy = true;
    var data = new FormData(form);
    data.set("code", code);
    fetch(form.action, { method: "POST", body: data, headers: { "Accept": "application/json" }, credentials: "same-origin" })
      .then(function (r) { return r.json().catch(function () { return { ok: false, message: "Check-in failed." }; }); })
      .then(function (res) {
        if (window.ChurchPlus && window.ChurchPlus.toast) { window.ChurchPlus.toast(res.message, res.ok ? "success" : "warning"); }
        input.value = "";
        input.focus();
      })
      .catch(function () { if (window.ChurchPlus) { window.ChurchPlus.toast("Network problem. Try again.", "danger"); } })
      .then(function () { busy = false; });
  }

  form.addEventListener("submit", function (ev) { ev.preventDefault(); submit(input.value.trim()); });

  var scanBtn = form.querySelector("[data-scan]");
  var video = form.querySelector("[data-scan-video]");
  var note = form.querySelector("[data-scan-unsupported]");
  if (!("BarcodeDetector" in window) || !navigator.mediaDevices) { if (note) { note.hidden = false; } return; }
  scanBtn.hidden = false;
  var stream = null, detector = null, last = "", lastAt = 0;

  function stop() {
    if (stream) { stream.getTracks().forEach(function (t) { t.stop(); }); stream = null; }
    video.hidden = true;
    scanBtn.querySelector("span").textContent = "Scan with camera";
  }

  function tick() {
    if (!stream) { return; }
    detector.detect(video).then(function (codes) {
      if (codes.length) {
        var value = codes[0].rawValue, now = Date.now();
        if (value !== last || now - lastAt > 4000) { last = value; lastAt = now; submit(value); }
      }
    }).catch(function () { /* frame not ready */ }).then(function () { setTimeout(tick, 250); });
  }

  scanBtn.addEventListener("click", function () {
    if (stream) { stop(); return; }
    detector = detector || new window.BarcodeDetector({ formats: ["qr_code"] });
    navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } }).then(function (s) {
      stream = s;
      video.srcObject = s;
      video.hidden = false;
      video.play();
      scanBtn.querySelector("span").textContent = "Stop camera";
      tick();
    }).catch(function () {
      if (window.ChurchPlus) { window.ChurchPlus.toast("Camera permission was not given.", "warning"); }
    });
  });
  window.addEventListener("pagehide", stop);
})();
