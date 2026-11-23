/* ChurchPlus camera: take passport-style photos of members and leaders.
 *
 * - Opens the device camera inside the page (front or back camera; switchable when there are two).
 * - An oval guide shows where the face goes; the photo is cropped to the same 4:5 passport shape
 *   used on profiles and ID cards, and saved at 800 x 1000 pixels.
 * - Warns when a photo looks too dark or too bright, then lets you retake it.
 * - Asks for a tick confirming the person agrees, then saves straight to their record.
 * - On Photo day, "Next person" moves to the next person on the page without closing the camera.
 * - If the camera can't be used (no camera, permission blocked, older browser), "Choose file"
 *   still works: on phones it offers to take a photo with the phone's own camera app.
 * The camera is switched off as soon as the window closes or the page is hidden.
 */
(function () {
  "use strict";
  var dlg = document.querySelector("[data-camera-dialog]");
  if (!dlg || !window.fetch || !window.FormData) { return; }

  var OUT_W = 800, OUT_H = 1000, RATIO = OUT_W / OUT_H;
  var $ = function (sel) { return dlg.querySelector(sel); };
  var video = $("[data-camera-video]"), still = $("[data-camera-still]"), guide = $("[data-camera-guide]");
  var flash = $("[data-camera-flash]"), tip = $("[data-camera-tip]");
  var msg = $("[data-camera-message]"), msgText = $("[data-camera-message-text]");
  var captureBtn = $("[data-camera-capture]"), switchBtn = $("[data-camera-switch]"), fileInput = $("[data-camera-file]");
  var consent = $("[data-camera-consent]"), saveBtn = $("[data-camera-save]"), retakeBtn = $("[data-camera-retake]");
  var nextBtn = $("[data-camera-next]"), doneText = $("[data-camera-done-text]");
  var DEFAULT_TIP = tip.textContent;

  var stream = null, facing = "user", current = null, blob = null, blobUrl = null, source = "camera", busy = false;
  var canUseCamera = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.isSecureContext);

  // "Upload photo" buttons that hold a file box send the form as soon as a file is chosen.
  Array.prototype.forEach.call(document.querySelectorAll("input[data-auto-submit]"), function (input) {
    input.addEventListener("change", function () { if (input.files && input.files.length) { input.form.submit(); } });
  });

  // Buttons stay hidden until this script runs, so the plain upload form is the fallback without JS.
  Array.prototype.forEach.call(document.querySelectorAll("[data-camera-open]"), function (b) { b.hidden = false; });

  function toast(text, kind) {
    if (window.ChurchPlus && window.ChurchPlus.toast) { window.ChurchPlus.toast(text, kind); }
  }

  function setTip(text, warn) {
    tip.textContent = text || DEFAULT_TIP;
    tip.classList.toggle("is-warning", !!warn);
  }

  function step(name) {
    Array.prototype.forEach.call(dlg.querySelectorAll("[data-camera-step]"), function (el) {
      el.hidden = el.getAttribute("data-camera-step") !== name;
    });
    dlg.setAttribute("data-step", name);
  }

  function showMessage(text) {
    msgText.textContent = text;
    msg.hidden = false;
    guide.hidden = true;
    video.hidden = true;
    captureBtn.disabled = true;
  }

  function stopCamera() {
    if (stream) { stream.getTracks().forEach(function (t) { t.stop(); }); stream = null; }
    video.srcObject = null;
  }

  function clearPhoto() {
    if (blobUrl) { URL.revokeObjectURL(blobUrl); blobUrl = null; }
    blob = null;
    still.hidden = true;
    still.removeAttribute("src");
  }

  function cameraError(err) {
    var name = err && err.name;
    if (name === "NotAllowedError" || name === "SecurityError") {
      return "Camera access is blocked. Allow the camera for this site in your browser settings, or use Choose file.";
    }
    if (name === "NotFoundError" || name === "OverconstrainedError") {
      return "No camera was found on this device. Use Choose file instead.";
    }
    if (name === "NotReadableError" || name === "AbortError") {
      return "The camera is busy in another app. Close that app and try again, or use Choose file.";
    }
    return "The camera couldn't start. Use Choose file instead.";
  }

  function startCamera() {
    clearPhoto();
    if (!canUseCamera) {
      showMessage("This browser can't open the camera here. Use Choose file: on a phone it lets you take a photo.");
      return;
    }
    if (stream) { video.hidden = false; guide.hidden = false; msg.hidden = true; captureBtn.disabled = false; return; }
    showMessage("Starting the camera…");
    msg.classList.add("is-loading");
    navigator.mediaDevices.getUserMedia({
      audio: false,
      video: { facingMode: { ideal: facing }, width: { ideal: 1280 }, height: { ideal: 1280 } }
    }).then(function (s) {
      if (!dlg.open) { s.getTracks().forEach(function (t) { t.stop(); }); return; }
      stream = s;
      video.srcObject = s;
      video.classList.toggle("is-mirrored", facing === "user");
      return video.play().catch(function () {}).then(function () {
        msg.hidden = true;
        msg.classList.remove("is-loading");
        video.hidden = false;
        guide.hidden = false;
        captureBtn.disabled = false;
        captureBtn.focus();
        if (navigator.mediaDevices.enumerateDevices) {
          navigator.mediaDevices.enumerateDevices().then(function (list) {
            var many = list.filter(function (d) { return d.kind === "videoinput"; }).length > 1;
            // Keep the button's space when there is one camera, so the round button stays centred.
            switchBtn.classList.toggle("is-unavailable", !many);
            switchBtn.disabled = !many;
            if (many) { switchBtn.removeAttribute("aria-hidden"); switchBtn.removeAttribute("tabindex"); }
            else { switchBtn.setAttribute("aria-hidden", "true"); switchBtn.setAttribute("tabindex", "-1"); }
          }).catch(function () {});
        }
      });
    }).catch(function (err) {
      msg.classList.remove("is-loading");
      showMessage(cameraError(err));
    });
  }

  // Crop the centre of an image or video frame to the 4:5 passport shape shown on screen.
  // (The live preview of a front camera is mirrored like a mirror; the saved photo is not,
  // so writing on clothing reads the right way round.)
  function crop(src, sw, sh) {
    var w, h;
    if (sw / sh > RATIO) { h = sh; w = sh * RATIO; } else { w = sw; h = sw / RATIO; }
    var canvas = document.createElement("canvas");
    canvas.width = Math.round(Math.min(OUT_W, w));
    canvas.height = Math.round(canvas.width / RATIO);
    canvas.getContext("2d").drawImage(src, (sw - w) / 2, (sh - h) / 2, w, h, 0, 0, canvas.width, canvas.height);
    return canvas;
  }

  function lightCheck(canvas) {
    try {
      var c = document.createElement("canvas");
      c.width = 32; c.height = 40;
      var ctx = c.getContext("2d");
      ctx.drawImage(canvas, 0, 0, 32, 40);
      var d = ctx.getImageData(0, 0, 32, 40).data, sum = 0;
      for (var i = 0; i < d.length; i += 4) { sum += 0.2126 * d[i] + 0.7152 * d[i + 1] + 0.0722 * d[i + 2]; }
      var avg = sum / (d.length / 4) / 255;
      if (avg < 0.22) { return "This photo looks dark. Move nearer to a window or light and retake it."; }
      if (avg > 0.88) { return "This photo looks very bright. Move away from strong light behind or above the person."; }
    } catch (e) { /* ignore */ }
    return "";
  }

  function review(canvas, from, note) {
    source = from;
    canvas.toBlob(function (b) {
      if (!b) { setTip("That photo couldn't be prepared. Try again.", true); return; }
      clearPhoto();
      blob = b;
      blobUrl = URL.createObjectURL(b);
      still.src = blobUrl;
      still.hidden = false;
      video.hidden = true;
      guide.hidden = true;
      msg.hidden = true;
      var warn = lightCheck(canvas);
      setTip(warn || note || "Check the photo. Is the face clear, centred and in focus?", !!warn);
      consent.checked = false;
      saveBtn.disabled = true;
      step("review");
      consent.focus();
    }, "image/jpeg", 0.9);
  }

  function capture() {
    if (!stream || !video.videoWidth) { return; }
    flash.classList.remove("is-on");
    void flash.offsetWidth;            // restart the flash animation
    flash.classList.add("is-on");
    review(crop(video, video.videoWidth, video.videoHeight), "camera");
  }

  function fromFile(file) {
    if (!file) { return; }
    var heic = /\.(heic|heif)$/i.test(file.name || "") || /hei[cf]/i.test(file.type);
    if (file.type && !/^image\//.test(file.type) && !heic) { setTip("Choose a photo from the camera or gallery.", true); return; }
    var url = URL.createObjectURL(file);
    var img = new Image();
    img.onload = function () {
      var canvas = crop(img, img.naturalWidth, img.naturalHeight);
      URL.revokeObjectURL(url);
      review(canvas, "upload", "The photo has been trimmed to passport shape. Check the face is fully in view.");
    };
    img.onerror = function () {
      URL.revokeObjectURL(url);
      // This browser can't show the picture (e.g. an iPhone HEIC photo on Android or Windows).
      // Send it as it is: ChurchPlus converts it, turns it upright and shrinks it on the server.
      clearPhoto();
      blob = file;
      source = "upload";
      still.hidden = true;
      video.hidden = true;
      guide.hidden = true;
      msg.hidden = true;
      setTip("This photo can't be previewed on this device, but it can still be saved. ChurchPlus will convert it.");
      consent.checked = false;
      saveBtn.disabled = true;
      step("review");
      consent.focus();
    };
    img.src = url;
  }

  function nextPerson() {
    var buttons = Array.prototype.slice.call(document.querySelectorAll("[data-camera-open]"));
    var i = buttons.indexOf(current ? current.button : null);
    for (var j = i + 1; j < buttons.length; j++) {
      if (!buttons[j].hidden && !buttons[j].closest("[data-photo-done]")) { return buttons[j]; }
    }
    return null;
  }

  function updatePage(url) {
    var sel = current.button.getAttribute("data-target");
    var nodes = sel ? document.querySelectorAll(sel) : [];
    Array.prototype.forEach.call(nodes, function (el) {
      if (el.tagName === "IMG") { el.src = url; return; }
      var img = document.createElement("img");
      img.className = el.getAttribute("data-photo-class") || "";
      img.alt = el.getAttribute("data-photo-alt") || "";
      img.src = url;
      if (el.id) { img.id = el.id; }
      el.parentNode.replaceChild(img, el);
    });
    var card = current.button.closest("[data-photo-card]");
    if (card) {
      card.setAttribute("data-photo-done", "");
      var label = current.button.querySelector("span");
      if (label) { label.textContent = "Retake"; }
    }
  }

  function save() {
    if (!blob || busy || !consent.checked) { return; }
    busy = true;
    saveBtn.disabled = true;
    saveBtn.classList.add("is-busy");
    var data = new FormData();
    data.append("csrf_token", dlg.getAttribute("data-csrf"));
    data.append("source", source);
    data.append("photo", blob, blob.name || "photo.jpg");
    fetch(current.upload, { method: "POST", body: data, credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (r) {
        return r.json().catch(function () {
          return { ok: false, message: r.status === 413 ? "That photo is too large. Try a smaller one." : "The photo couldn't be saved. Try again." };
        });
      })
      .then(function (res) {
        if (!res.ok) { setTip(res.message || "The photo couldn't be saved.", true); saveBtn.disabled = !consent.checked; return; }
        if (res.photo_url) { updatePage(res.photo_url); }
        doneText.textContent = res.message || "Photo saved.";
        var next = nextPerson();
        nextBtn.hidden = !next;
        step("done");
        setTip(next ? "Ready for the next person." : "All done on this page.");
        (next ? nextBtn : dlg.querySelector('[data-camera-step="done"] [data-camera-close]')).focus();
        toast(res.message || "Photo saved.", "success");
      })
      .catch(function () { setTip("No connection. Check the internet and press Save again.", true); saveBtn.disabled = !consent.checked; })
      .then(function () { busy = false; saveBtn.classList.remove("is-busy"); });
  }

  function open(button) {
    current = { button: button, upload: button.getAttribute("data-upload"), name: button.getAttribute("data-name") || "" };
    Array.prototype.forEach.call(dlg.querySelectorAll("[data-camera-name]"), function (el) { el.textContent = current.name; });
    setTip("");
    consent.checked = false;
    saveBtn.disabled = true;
    fileInput.value = "";
    step("live");
    if (!dlg.open) { dlg.showModal(); }
    startCamera();
  }

  function close() {
    stopCamera();
    clearPhoto();
    if (dlg.open) { dlg.close(); }
    if (current && current.button && document.body.contains(current.button)) { current.button.focus(); }
  }

  document.addEventListener("click", function (ev) {
    var opener = ev.target.closest("[data-camera-open]");
    if (opener) { ev.preventDefault(); open(opener); }
  });
  dlg.addEventListener("click", function (ev) {
    if (ev.target.closest("[data-camera-close]")) { ev.preventDefault(); close(); }
    else if (ev.target === dlg) { close(); }       // tap on the dimmed backdrop
  });
  // Switch the camera off however the window closes (×, Escape, Back button, backdrop). Some
  // browsers skip the "close" event, so also watch the dialog's open flag itself.
  dlg.addEventListener("close", function () { stopCamera(); clearPhoto(); });
  dlg.addEventListener("cancel", function () { stopCamera(); });
  if (window.MutationObserver) {
    new MutationObserver(function () { if (!dlg.open) { stopCamera(); clearPhoto(); } })
      .observe(dlg, { attributes: true, attributeFilter: ["open"] });
  }
  captureBtn.addEventListener("click", capture);
  retakeBtn.addEventListener("click", function () { setTip(""); step("live"); startCamera(); });
  consent.addEventListener("change", function () { saveBtn.disabled = !consent.checked || !blob; });
  saveBtn.addEventListener("click", save);
  fileInput.addEventListener("change", function () { fromFile(fileInput.files && fileInput.files[0]); });
  switchBtn.addEventListener("click", function () {
    facing = facing === "user" ? "environment" : "user";
    stopCamera();
    startCamera();
  });
  nextBtn.addEventListener("click", function () {
    var next = nextPerson();
    if (next) { next.scrollIntoView({ block: "center" }); open(next); } else { close(); }
  });
  // Space or Enter on the preview takes the photo (handy with a keyboard or a Bluetooth clicker).
  dlg.addEventListener("keydown", function (ev) {
    if ((ev.key === " " || ev.key === "Enter") && dlg.getAttribute("data-step") === "live" &&
        ev.target.tagName !== "BUTTON" && ev.target.tagName !== "INPUT" && !captureBtn.disabled) {
      ev.preventDefault();
      capture();
    }
  });
  // Turn the camera off when the app goes to the background; turn it back on when it returns.
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) { stopCamera(); }
    else if (dlg.open && dlg.getAttribute("data-step") === "live") { startCamera(); }
  });
})();
