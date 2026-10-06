/* SQ-tool web interface: capture, library and side-by-side comparison. No dependencies. */
(function () {
  "use strict";

  var PALETTE = ["#2563eb", "#e11d48", "#16a34a", "#d97706", "#7c3aed", "#0891b2", "#db2777", "#64748b"];
  var S = {
    view: "capture", state: null, items: [], selected: load("sq.selected", []), device: load("sq.device", null),
    details: {}, nulls: {}, savedSeen: "", lastDevices: "", lastLive: "", charts: []
  };

  // ---------------------------------------------------------------- helpers

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $all(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function load(key, fallback) {
    try { var v = localStorage.getItem(key); return v == null ? fallback : JSON.parse(v); } catch (e) { return fallback; }
  }
  function save(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* private mode */ } }

  function api(path, opts) {
    opts = opts || {};
    var init = { method: opts.method || "GET", headers: {} };
    if (opts.json !== undefined) { init.body = JSON.stringify(opts.json); init.headers["Content-Type"] = "application/json"; }
    if (opts.body !== undefined) { init.body = opts.body; init.headers["Content-Type"] = "application/octet-stream"; }
    return fetch(path, init).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) { throw new Error(data.error || ("HTTP " + r.status)); }
        return data;
      });
    });
  }

  var toastTimer = null;
  function toast(msg) {
    var t = $("#toast");
    t.textContent = msg; t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.hidden = true; }, 4000);
  }

  function fmtRate(r) { if (!r) return "–"; var k = r / 1000; return (k % 1 === 0 ? k.toFixed(0) : k.toFixed(1)) + " kHz"; }
  function fmtSecs(s) {
    if (s == null) return "–";
    if (s < 60) return s.toFixed(s < 10 ? 2 : 1) + " s";
    var m = Math.floor(s / 60), sec = Math.floor(s % 60);
    return m + ":" + (sec < 10 ? "0" : "") + sec;
  }
  function fmtClock(s) {
    s = Math.max(0, s || 0);
    var m = Math.floor(s / 60), sec = s % 60;
    return (m < 10 ? "0" : "") + m + ":" + (sec < 10 ? "0" : "") + sec.toFixed(1);
  }
  function fmtTime(iso) {
    // Stored with a UTC offset; shown in this device's own time zone.
    var d = iso ? new Date(iso) : null;
    if (!d || isNaN(d.getTime())) return iso ? String(iso).replace("T", " ").slice(0, 16) : "–";
    return d.toLocaleString([], { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }
  function fmtDb(v) { return v == null ? "−∞" : (v > 0 ? "+" : "") + v.toFixed(1); }
  function fmtNum(n) { return n == null ? "–" : Number(n).toLocaleString(); }
  function fmtBytes(n) {
    if (n == null) return "–";
    var u = ["B", "KB", "MB", "GB", "TB"], i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return n.toFixed(i ? 1 : 0) + " " + u[i];
  }
  function fpColor(fp) {
    if (!fp) return "transparent";
    return "hsl(" + (parseInt(fp.slice(0, 6), 16) % 360) + " 70% 48%)";
  }
  function itemById(id) { return S.items.filter(function (i) { return i.id === id; })[0]; }
  function formatLine(it) {
    var f = it.alsa_format || (it.label || "").replace(/^WAV /, "");
    return f + " · " + fmtRate(it.rate) + (it.channels && it.channels !== 2 ? " · " + it.channels + " ch" : "");
  }

  // ---------------------------------------------------------------- tabs

  function show(view) {
    S.view = view;
    $all(".tabs button").forEach(function (b) { b.setAttribute("aria-selected", String(b.dataset.view === view)); });
    $all(".view").forEach(function (v) { v.hidden = v.id !== "view-" + view; });
    if (view === "library") renderLibrary();
    if (view === "compare") renderCompare();
    renderSelbar();
    window.scrollTo(0, 0);
  }
  $all(".tabs button").forEach(function (b) { b.addEventListener("click", function () { show(b.dataset.view); }); });

  // ---------------------------------------------------------------- capture view

  function buildCapture() {
    $("#view-capture").innerHTML =
      '<div id="problems"></div>' +
      '<div class="card"><h2>1. Output device</h2><div id="devices" class="stack"></div></div>' +
      '<div class="card stack"><h2>2. Capture</h2>' +
      '<label class="field">Name this capture<input id="cap-name" type="text" maxlength="200" ' +
      'placeholder="e.g. Roon – test track 24/96" autocomplete="off"></label>' +
      '<div class="grid2"><label class="field">Stop after this many seconds without music' +
      '<input id="cap-idle" type="number" min="0" step="1" value="5" inputmode="numeric"></label>' +
      '<label class="field">Longest capture (minutes)' +
      '<input id="cap-max" type="number" min="1" step="1" value="20" inputmode="numeric"></label></div>' +
      '<div id="cap-live" aria-live="polite"></div></div>' +
      '<div class="card"><h2>How it works</h2><ol class="lines">' +
      "<li>Pick the DAC above, give the capture a name (for example the player and track), and press <b>Start capture</b>.</li>" +
      "<li>Play a track in Roon, Mandarin or any other player, to the same DAC, as you normally would.</li>" +
      "<li>When the music stops, the capture is saved and analysed. Repeat with the other player.</li>" +
      "<li>In <b>Library</b>, tick two or more captures (and the source file, if you imported it) and press <b>Compare</b>.</li>" +
      "</ol><p class=\"muted\">With a USB DAC, SQ-tool records the USB packets sent to the DAC (Linux usbmon), so the players " +
      "need no changes and nothing in the signal path is touched.</p></div>";
    $("#cap-name").value = load("sq.name", "");
    $("#cap-name").addEventListener("input", function (e) { save("sq.name", e.target.value); });
    $("#devices").addEventListener("change", function (e) {
      if (e.target.name === "device") { S.device = e.target.value; save("sq.device", S.device); S.lastDevices = ""; renderCaptureLive(); }
    });
  }

  function renderCaptureLive() {
    var st = S.state;
    if (!st) return;
    var probs = (st.problems || []).map(function (p) { return '<div class="alert warn">' + esc(p) + "</div>"; }).join("");
    if ($("#problems").innerHTML !== probs) $("#problems").innerHTML = probs;

    var devs = st.devices || [];
    if (!devs.some(function (d) { return d.id === S.device; }) && devs.length) {
      var usb = devs.filter(function (d) { return d.kind === "usb"; })[0];
      S.device = (usb || devs[0]).id;
    }
    var dkey = JSON.stringify([devs, S.device]);
    if (dkey !== S.lastDevices) {
      S.lastDevices = dkey;
      $("#devices").innerHTML = devs.length ? devs.map(function (d) {
        var p = d.playing, now;
        if (p && p.format && ["RUNNING", "PREPARED", "DRAINING", "PAUSED"].indexOf(p.state) >= 0) {
          now = '<span class="dot on"></span>' + (p.state === "PAUSED" ? "Paused: " : "Playing ") + esc(p.format) + " · " +
            fmtRate(p.rate) + " · " + p.channels + " ch" + (p.player ? " — sent by <b>" + esc(p.player) + "</b>" : "");
        } else {
          now = '<span class="dot"></span>Idle';
        }
        return '<label class="device' + (d.id === S.device ? " selected" : "") + '"><input type="radio" name="device" value="' +
          esc(d.id) + '"' + (d.id === S.device ? " checked" : "") + '><div><div class="name">' + esc(d.name) + "</div>" +
          '<div class="muted" style="font-size:.82rem">' + esc(d.detail) + "</div>" +
          '<div class="now">' + now + "</div></div></label>";
      }).join("") : '<p class="muted">No playback devices found yet.</p>';
    }

    var cap = st.capture, html;
    var active = cap && ["starting", "waiting", "recording", "stopping"].indexOf(cap.state) >= 0;
    var saving = cap && cap.saving > 0;
    $("#rec-badge").hidden = !(cap && cap.state === "recording");
    if (active) {
      var take = cap.take;
      if (cap.state === "recording" && take) {
        html = '<div class="alert ok"><span class="dot rec"></span><b>Recording</b> ' + esc(cap.request.name) + "</div>" +
          '<div class="status-big">' + fmtClock(take.seconds) + "</div>" +
          '<dl class="kv"><dt>Format</dt><dd>' + esc(take.format) + " · " + fmtRate(take.rate) + " · " + take.channels + " ch</dd>" +
          (take.player && take.player.name ? "<dt>Sent by</dt><dd>" + esc(take.player.name) + "</dd>" : "") +
          (take.interruptions ? "<dt>Interruptions</dt><dd>" + take.interruptions + " (the stream stopped and restarted)</dd>" : "") +
          (cap.usbmon_dropped ? '<dt>Warning</dt><dd style="color:var(--bad)">' + cap.usbmon_dropped + " USB events were lost: the capture may be incomplete</dd>" : "") +
          "</dl>";
      } else if (cap.state === "stopping") {
        html = '<div class="alert ok"><span class="spinner"></span> Finishing…</div>';
      } else {
        html = '<div class="alert ok"><span class="spinner"></span> <b>Waiting for playback</b> — start playing now. ' +
          "It records automatically and stops " + (cap.request.idle_stop ? cap.request.idle_stop + " s after the music ends." : "when you press Stop.") + "</div>";
      }
      html += '<button class="btn big rec" id="stop-btn">Stop</button>';
    } else {
      html = '<button class="btn big primary" id="start-btn"' + (S.device ? "" : " disabled") + ">Start capture</button>";
      if (cap) {
        if (saving) html += '<div class="alert ok"><span class="spinner"></span> Saving and analysing…</div>';
        (cap.errors || []).forEach(function (e) { html += '<div class="alert bad">' + esc(e) + "</div>"; });
        if (cap.saved && cap.saved.length) {
          html += '<div class="stack"><h3>Saved</h3>' + cap.saved.map(function (it) {
            return '<div class="row spread"><div><b>' + esc(it.name) + '</b><br><span class="muted">' + esc(formatLine(it)) +
              " · " + fmtSecs(it.duration) + '</span></div><button class="btn small" data-open="' + esc(it.id) + '">Open</button></div>';
          }).join("") + "</div>";
        } else if (cap.state === "done" && !saving && !(cap.errors || []).length) {
          html += '<p class="muted">Nothing was recorded' + (cap.stop_reason ? " (" + esc(cap.stop_reason) + ")" : "") + ".</p>";
        }
      }
    }
    if (html !== S.lastLive) { S.lastLive = html; $("#cap-live").innerHTML = html; }
  }

  document.addEventListener("click", function (e) {
    var t = e.target.closest ? e.target.closest("button, [data-open], [data-close]") : null;
    if (!t) return;
    if (t.id === "start-btn") startCapture();
    else if (t.id === "stop-btn") api("/api/capture/stop", { method: "POST" }).then(poll).catch(function (err) { toast(err.message); });
    else if (t.dataset.open) openItem(t.dataset.open);
    else if (t.hasAttribute("data-close")) closeSheet();
  });

  function startCapture() {
    var body = {
      device: S.device, name: $("#cap-name").value,
      idle_stop: parseFloat($("#cap-idle").value) || 0,
      max_seconds: (parseFloat($("#cap-max").value) || 20) * 60
    };
    $("#start-btn").disabled = true;
    api("/api/capture", { method: "POST", json: body }).then(poll).catch(function (err) { toast(err.message); poll(); });
  }

  // ---------------------------------------------------------------- library view

  function renderLibrary() {
    var v = $("#view-library");
    var items = S.items;
    var free = S.state && S.state.free_bytes != null ? " · " + fmtBytes(S.state.free_bytes) + " free" : "";
    var html = '<div class="card row spread"><div><h2>Library</h2><span class="muted">' + items.length +
      " item" + (items.length === 1 ? "" : "s") + free + ". Same colour swatch = identical sample data.</span></div>" +
      '<div class="row"><label class="btn small">Import source file<input id="import" type="file" accept=".wav,.flac,.aif,.aiff,.m4a,audio/*" hidden></label>' +
      '<button class="btn small" id="tracks-btn">Test tracks</button></div></div>';
    if (!items.length) {
      html += '<div class="card empty">No captures yet. Make one on the Capture tab, or import a source file to compare against.</div>';
    } else {
      html += '<div class="items">' + items.map(function (it) {
        var sel = S.selected.indexOf(it.id) >= 0;
        return '<div class="item' + (sel ? " selected" : "") + '"><input type="checkbox" data-select="' + esc(it.id) + '"' +
          (sel ? " checked" : "") + ' aria-label="Select ' + esc(it.name) + ' for comparison">' +
          '<div data-open="' + esc(it.id) + '" style="cursor:pointer"><div class="title"><span class="swatch" style="background:' +
          fpColor(it.fingerprint) + '"></span> ' + esc(it.name) + '</div><div class="chips">' +
          '<span class="chip ' + esc(it.kind) + '">' + (it.kind === "capture" ? "Capture" : "Source") + "</span>" +
          '<span class="chip">' + esc(formatLine(it)) + "</span>" +
          '<span class="chip">' + fmtSecs(it.duration) + "</span>" +
          (it.player ? '<span class="chip">' + esc(it.player) + "</span>" : "") +
          (it.fingerprint ? '<span class="chip fp">' + esc(it.fingerprint.slice(0, 8)) + "</span>" : "") +
          '<span class="chip">' + esc(fmtTime(it.created)) + "</span>" +
          '</div></div><button class="open" data-open="' + esc(it.id) + '" aria-label="Details">›</button></div>';
      }).join("") + "</div>";
    }
    v.innerHTML = html;
    $("#import").addEventListener("change", importFile);
    $("#tracks-btn").addEventListener("click", openTracks);
    $all("[data-select]", v).forEach(function (cb) {
      cb.addEventListener("change", function () { toggleSelect(cb.dataset.select, cb.checked); });
    });
  }

  function toggleSelect(id, on) {
    S.selected = S.selected.filter(function (x) { return x !== id; });
    if (on) S.selected.push(id);
    save("sq.selected", S.selected);
    $all('[data-select="' + id + '"]').forEach(function (cb) { cb.checked = on; cb.closest(".item").classList.toggle("selected", on); });
    renderSelbar();
  }

  function renderSelbar() {
    var bar = $("#selbar");
    var n = S.selected.length;
    $("#selcount").textContent = n ? "(" + n + ")" : "";
    if (!bar) {
      bar = document.createElement("div");
      bar.id = "selbar"; bar.className = "selbar";
      document.body.appendChild(bar);
      bar.addEventListener("click", function (e) {
        if (e.target.id === "sel-clear") { S.selected = []; save("sq.selected", []); renderLibrary(); renderSelbar(); }
        if (e.target.id === "sel-compare") show("compare");
      });
    }
    bar.hidden = !(n && S.view === "library");
    bar.innerHTML = "<span><b>" + n + "</b> selected</span><span class=\"row\"><button class=\"btn small\" id=\"sel-clear\">Clear</button>" +
      '<button class="btn small primary" id="sel-compare"' + (n < 2 ? " disabled" : "") + ">Compare</button></span>";
  }

  function importFile(e) {
    var file = e.target.files[0];
    if (!file) return;
    var name = prompt("Name for this source file", file.name.replace(/\.[^.]+$/, ""));
    if (name === null) return;
    toast("Uploading " + file.name + "…");
    api("/api/import?name=" + encodeURIComponent(name) + "&filename=" + encodeURIComponent(file.name),
      { method: "POST", body: file }).then(function (it) {
      toast("Imported " + it.name);
      return refreshItems();
    }).catch(function (err) { toast("Import failed: " + err.message); });
  }

  function refreshItems() {
    return api("/api/items").then(function (items) {
      S.items = items;
      var ids = items.map(function (i) { return i.id; });
      S.selected = S.selected.filter(function (x) { return ids.indexOf(x) >= 0; });
      save("sq.selected", S.selected);
      $("#count").textContent = items.length ? "(" + items.length + ")" : "";
      if (S.view === "library") renderLibrary();
      if (S.view === "compare") renderCompare();
      renderSelbar();
    });
  }

  // ---------------------------------------------------------------- item sheet

  function openSheet(html) {
    $("#sheet-body").innerHTML = html;
    $("#sheet").hidden = false;
    document.body.style.overflow = "hidden";
  }
  function closeSheet() {
    $("#sheet").hidden = true;
    document.body.style.overflow = "";
    S.charts = S.charts.filter(function (c) { return document.body.contains(c.canvas) && !$("#sheet").contains(c.canvas); });
  }

  function getItem(id) {
    if (S.details[id]) return Promise.resolve(S.details[id]);
    return api("/api/items/" + encodeURIComponent(id)).then(function (d) { S.details[id] = d; return d; });
  }

  function captureRows(a) {
    var cap = a.capture, cpu = a.cpu, rows = [];
    if (!cap) return rows;
    rows.push(["Recorded", cap.method === "usbmon" ? "USB packets to " + esc((cap.device || {}).name || "the DAC") + " (usbmon)" : "ALSA loopback " + esc(cap.playback_device || "")]);
    if (cap.player && cap.player.name) rows.push(["Sent by", esc(cap.player.name) + " (pid " + cap.player.pid + ")"]);
    if (cap.period_size) rows.push(["Player I/O", esc(cap.alsa_format) + ", period " + cap.period_size + " frames (" + (1000 * cap.period_size / cap.rate).toFixed(1) + " ms), buffer " + cap.buffer_size + " frames (" + (1000 * cap.buffer_size / cap.rate).toFixed(1) + " ms)"]);
    rows.push(["Started", esc(fmtTime(cap.started))]);
    rows.push(["Stopped", esc(cap.stop_reason || "–")]);
    var usb = cap.usb;
    if (usb) {
      var fpp = Object.keys(usb.frames_per_packet || {}).map(function (k) { return k + "×" + fmtNum(usb.frames_per_packet[k]); }).join(", ");
      rows.push(["USB transfers", fmtNum(usb.urbs) + " transfers, " + fmtNum(usb.packets) + " packets (frames per packet: " + esc(fpp) + ")"]);
      if (usb.dac_rate_set) rows.push(["DAC clock set to", fmtNum(usb.dac_rate_set) + " Hz"]);
      var problems = [];
      if (usb.usbmon_dropped) problems.push(usb.usbmon_dropped + " USB events lost by the monitor");
      if (usb.uncaptured_transfers) problems.push(usb.uncaptured_transfers + " transfers without data");
      if (usb.iso_errors || usb.packet_errors) problems.push((usb.iso_errors + usb.packet_errors) + " USB packet errors");
      if (usb.bad_packets) problems.push(usb.bad_packets + " packets not made of whole frames");
      rows.push(["Integrity", problems.length ? '<span style="color:var(--bad)">' + esc(problems.join("; ")) + "</span>" : "complete, no USB errors"]);
    } else if (cap.capture_overruns !== undefined) {
      rows.push(["Integrity", cap.capture_overruns ? '<span style="color:var(--bad)">' + cap.capture_overruns + " capture overruns: incomplete</span>" : "no capture overruns"]);
    }
    var ints = cap.interruptions || [];
    if (ints.length) rows.push(["Interruptions", ints.slice(0, 8).map(function (i) { return "at " + i.seconds.toFixed(2) + " s (" + (1000 * i.gap).toFixed(0) + " ms)"; }).join(", ") + (ints.length > 8 ? " …" : "")]);
    if (cap.player_xruns_seen) rows.push(["Player underruns", String(cap.player_xruns_seen)]);
    if (cpu && cpu.system_percent != null) {
      rows.push(["CPU while playing", cpu.system_percent.toFixed(1) + "% of " + cpu.cpus + " CPUs; " +
        (cpu.top || []).slice(0, 4).map(function (t) { return esc(t.name) + " " + t.percent_of_one_cpu.toFixed(1) + "%"; }).join(", ")]);
    }
    return rows;
  }

  function analysisRows(a) {
    var rows = [];
    rows.push(["Format", esc(a.label) + ", " + a.channels + " ch, " + fmtRate(a.rate)]);
    rows.push(["Length", fmtSecs(a.duration) + " (" + fmtNum(a.frames) + " frames)"]);
    if (a.silent) { rows.push(["Content", "digital silence only"]); return rows; }
    if (a.is_float) rows.push(["Resolution", a.resolution ? "exact " + a.resolution + "-bit values in floating point" : "full floating point"]);
    else rows.push(["Bits in use", a.resolution + " of " + a.bits + (a.resolution < a.bits ? " (low " + (a.bits - a.resolution) + " bits always zero)" : "")]);
    if (a.dop) rows.push(["DSD", esc(a.dop)]);
    rows.push(["Silence", fmtSecs(a.lead_silence / a.rate) + " at the start, " + fmtSecs(a.trail_silence / a.rate) + " at the end"]);
    rows.push(["Peak", (a.channel_stats || []).map(function (c, i) { return "ch" + (i + 1) + " " + fmtDb(c.peak_db) + " dBFS"; }).join(" · ")]);
    rows.push(["RMS", (a.channel_stats || []).map(function (c, i) { return "ch" + (i + 1) + " " + fmtDb(c.rms_db) + " dBFS"; }).join(" · ")]);
    var clipped = (a.channel_stats || []).reduce(function (s, c) { return s + c.clipped; }, 0);
    rows.push(["Clipping", clipped ? fmtNum(clipped) + " samples at full scale" : "none"]);
    rows.push(["Fingerprint", '<span class="swatch" style="background:' + fpColor(a.fingerprint) + '"></span> <span class="fp">' + esc(a.fingerprint || "–") + "</span>"]);
    return rows;
  }

  function kv(rows) {
    return '<dl class="kv">' + rows.map(function (r) { return "<dt>" + r[0] + "</dt><dd>" + r[1] + "</dd>"; }).join("") + "</dl>";
  }

  function openItem(id) {
    closeSheet();
    getItem(id).then(function (d) {
      var m = d.meta, a = d.analysis;
      var html = '<div class="stack"><div><span class="chip ' + esc(m.kind) + '">' + (m.kind === "capture" ? "Capture" : "Source file") + "</span>" +
        '<h2 id="sheet-title" style="margin-top:6px">' + esc(m.name) + "</h2></div>" +
        '<div class="card stack"><label class="field">Name<input id="ed-name" type="text" value="' + esc(m.name) + '"></label>' +
        '<label class="field">Notes<textarea id="ed-notes" placeholder="Settings used, track, anything worth remembering">' + esc(m.notes || "") + "</textarea></label>" +
        '<div class="row"><button class="btn small primary" id="ed-save">Save</button>' +
        '<a class="btn small" href="/api/items/' + encodeURIComponent(id) + '/audio">Download WAV</a>' +
        '<button class="btn small danger" id="ed-delete">Delete</button></div></div>' +
        '<div class="card"><h2>Analysis</h2>' + kv(analysisRows(a)) + "</div>";
      var crow = captureRows(a);
      if (crow.length) html += '<div class="card"><h2>Capture details</h2>' + kv(crow) + "</div>";
      if (m.source && m.source.filename) html += '<div class="card"><h2>Source</h2>' + kv([["File", esc(m.source.filename)], ["Decoded from", esc(m.source.format || "–")]]) + "</div>";
      var p = a.plots || {};
      html += '<div class="card"><h2>Spectrum</h2><canvas class="chart" id="it-spec"></canvas></div>' +
        '<div class="card"><h2>Level over time</h2><canvas class="chart" id="it-env"></canvas></div>';
      if (p.bits) html += '<div class="card"><h2>Bit usage</h2>' + bitsHtml([{ name: m.name, bits: p.bits, color: PALETTE[0] }]) + "</div>";
      openSheet(html + "</div>");
      if (p.spectrum && p.spectrum.freqs) spectrumChart($("#it-spec"), [{ label: m.name, color: PALETTE[0], x: p.spectrum.freqs, y: p.spectrum.db }]);
      if (p.envelope && p.envelope.t) timeChart($("#it-env"), [{ label: m.name, color: PALETTE[0], x: p.envelope.t, y: p.envelope.db }], "dBFS");
      $("#ed-save").addEventListener("click", function () {
        api("/api/items/" + encodeURIComponent(id), { method: "PATCH", json: { name: $("#ed-name").value, notes: $("#ed-notes").value } })
          .then(function () { delete S.details[id]; toast("Saved"); return refreshItems(); }).catch(function (e) { toast(e.message); });
      });
      $("#ed-delete").addEventListener("click", function () {
        if (!confirm("Delete “" + m.name + "” and its audio?")) return;
        api("/api/items/" + encodeURIComponent(id), { method: "DELETE" }).then(function () {
          delete S.details[id]; closeSheet(); toast("Deleted"); return refreshItems();
        }).catch(function (e) { toast(e.message); });
      });
    }).catch(function (e) { toast(e.message); });
  }

  function openTracks() {
    api("/api/test-tracks").then(function (d) {
      var list = function (tracks) {
        return tracks.length ? "<ul class=\"lines\">" + tracks.map(function (t) {
          return '<li><a href="/api/test-tracks/' + encodeURIComponent(t.name) + '">' + esc(t.name) + "</a> <span class=\"muted\">(" + fmtBytes(t.size) + ")</span></li>";
        }).join("") + "</ul>" : '<p class="muted">None yet.</p>';
      };
      openSheet('<div class="stack"><h2 id="sheet-title">Test tracks</h2><div class="card stack">' +
        "<p>15-second WAV files (16/44.1, 24/44.1, 24/96, 24/192) with silence, noise, tones and sweeps. Every kind of change to the data shows up in them, and they are added to the library as sources so captures can be checked against them.</p>" +
        "<p>Play them through each player: put them in your music library. When the container's <code>/data/test-tracks</code> folder is mapped into your music folder (see the README), they appear there by themselves; otherwise download them below.</p>" +
        '<button class="btn primary" id="make-tracks">Create test tracks</button><div id="track-list">' + list(d.tracks) + "</div></div>" +
        '<p class="muted">Test signals, not music: keep the volume low if they play through speakers.</p></div>');
      $("#make-tracks").addEventListener("click", function () {
        $("#make-tracks").disabled = true;
        $("#make-tracks").innerHTML = '<span class="spinner"></span> Creating…';
        api("/api/test-tracks", { method: "POST" }).then(function (r) {
          $("#track-list").innerHTML = list(r.tracks);
          $("#make-tracks").textContent = "Done";
          return refreshItems();
        }).catch(function (e) { toast(e.message); });
      });
    }).catch(function (e) { toast(e.message); });
  }

  // ---------------------------------------------------------------- compare view

  function verdictClass(v) {
    if (v === "IDENTICAL" || v === "PARTIAL") return "good";
    if (v === "GAPS" || v === "ALTERED" || v === "NO SIGNAL") return "meh";
    return "bad";
  }

  function pairs(ids) {
    var out = [];
    for (var i = 0; i < ids.length; i++) {
      for (var j = i + 1; j < ids.length; j++) {
        var a = ids[i], b = ids[j];
        var ia = itemById(a), ib = itemById(b);
        if (ib && ia && ib.kind === "reference" && ia.kind !== "reference") { var t = a; a = b; b = t; }
        out.push([a, b]);
      }
    }
    return out;
  }

  function renderCompare() {
    var v = $("#view-compare");
    var ids = S.selected.filter(function (id) { return itemById(id); });
    if (ids.length < 2) {
      v.innerHTML = '<div class="card empty"><p>Select two or more captures in the <b>Library</b> to compare them side by side and null them against each other.</p>' +
        '<button class="btn primary" id="goto-lib">Go to Library</button></div>';
      $("#goto-lib").addEventListener("click", function () { show("library"); });
      return;
    }
    v.innerHTML = '<div class="card"><span class="spinner"></span> Loading…</div>';
    Promise.all(ids.map(getItem)).then(function (docs) {
      if (S.view !== "compare") return;
      var cols = docs.map(function (d, i) { return { id: ids[i], meta: d.meta, a: d.analysis, color: PALETTE[i % PALETTE.length] }; });
      var html = '<div class="card"><h2>Side by side</h2><div class="table-wrap"><table class="cmp"><thead><tr><th></th>' +
        cols.map(function (c) { return '<th><div class="colhead"><span class="swatch" style="background:' + c.color + '"></span>' + esc(c.meta.name) + "</div></th>"; }).join("") +
        "</tr></thead><tbody>" + compareRows(cols) + "</tbody></table></div>" +
        '<p class="muted" style="font-size:.82rem">Highlighted cells differ from the first column.</p></div>';
      html += '<div class="card"><h2>Null tests</h2><p class="muted">Each pair is aligned sample by sample and subtracted. Identical data leaves nothing behind.</p><div class="stack" id="nulls"></div></div>';
      var withBits = cols.filter(function (c) { return c.a.plots && c.a.plots.bits; });
      if (withBits.length) html += '<div class="card"><h2>Bit usage</h2><p class="muted">How often each bit of the sample word is set (MSB left). Padding shows as empty bits; dither fills the low bits.</p>' +
        bitsHtml(withBits.map(function (c) { return { name: c.meta.name, bits: c.a.plots.bits, color: c.color }; })) + "</div>";
      html += '<div class="card"><h2>Spectrum</h2><canvas class="chart" id="cmp-spec"></canvas><div class="legend" id="leg-spec"></div></div>' +
        '<div class="card"><h2>Level over time</h2><canvas class="chart" id="cmp-env"></canvas><div class="legend" id="leg-env"></div></div>';
      v.innerHTML = html;
      // Identical sample data draws identical curves, one hidden under the other: say so.
      var labels = cols.map(function (c, i) {
        var twin = cols.slice(0, i).filter(function (o) { return o.a.fingerprint && o.a.fingerprint === c.a.fingerprint; })[0];
        return twin ? c.meta.name + " (same data as " + twin.meta.name + ")" : c.meta.name;
      });
      var spec = cols.map(function (c, i) {
        var p = c.a.plots && c.a.plots.spectrum;
        return p && p.freqs ? { label: labels[i], color: c.color, x: p.freqs, y: p.db } : null;
      }).filter(Boolean);
      var env = cols.map(function (c, i) {
        var p = c.a.plots && c.a.plots.envelope;
        return p && p.t ? { label: labels[i], color: c.color, x: p.t, y: p.db } : null;
      }).filter(Boolean);
      spectrumChart($("#cmp-spec"), spec); legend($("#leg-spec"), spec);
      timeChart($("#cmp-env"), env, "dBFS"); legend($("#leg-env"), env);
      runNulls(pairs(ids));
    }).catch(function (e) { v.innerHTML = '<div class="card alert bad">' + esc(e.message) + "</div>"; });
  }

  function compareRows(cols) {
    function cap(c) { return c.a.capture || {}; }
    var defs = [
      ["Type", function (c) { return c.meta.kind === "capture" ? "Capture" : "Source file"; }],
      ["Sent by", function (c) { return esc((cap(c).player || {}).name || "–"); }],
      ["Format", function (c) { return esc(cap(c).alsa_format || c.a.label); }],
      ["Sample rate", function (c) { return fmtRate(c.a.rate); }],
      ["Channels", function (c) { return String(c.a.channels); }],
      ["Bits in use", function (c) { return c.a.is_float ? (c.a.resolution ? c.a.resolution + " (float)" : "float") : c.a.resolution + " of " + c.a.bits; }],
      ["Length", function (c) { return fmtSecs(c.a.duration); }],
      ["Fingerprint", function (c) { return '<span class="swatch" style="background:' + fpColor(c.a.fingerprint) + '"></span> <span class="fp">' + esc((c.a.fingerprint || "–").slice(0, 12)) + "</span>"; }],
      ["Peak", function (c) { return (c.a.channel_stats || []).map(function (s) { return fmtDb(s.peak_db); }).join(" / ") + " dBFS"; }],
      ["RMS", function (c) { return (c.a.channel_stats || []).map(function (s) { return fmtDb(s.rms_db); }).join(" / ") + " dBFS"; }],
      ["Clipping", function (c) { var n = (c.a.channel_stats || []).reduce(function (s, x) { return s + x.clipped; }, 0); return n ? fmtNum(n) : "none"; }],
      ["Silence start / end", function (c) { return fmtSecs(c.a.lead_silence / c.a.rate) + " / " + fmtSecs(c.a.trail_silence / c.a.rate); }],
      ["DSD", function (c) { return esc(c.a.dop || "–"); }],
      ["Player period / buffer", function (c) { var p = cap(c); return p.period_size ? (1000 * p.period_size / p.rate).toFixed(1) + " / " + (1000 * p.buffer_size / p.rate).toFixed(1) + " ms" : "–"; }],
      ["USB packets", function (c) { var u = cap(c).usb; return u ? fmtNum(u.packets) + " (" + Object.keys(u.frames_per_packet || {}).join("/") + " frames)" : "–"; }],
      ["Interruptions", function (c) { var p = cap(c); return p.interruptions ? String(p.interruptions.length) : "–"; }],
      ["Capture integrity", function (c) {
        var p = cap(c), u = p.usb;
        if (u) return (u.usbmon_dropped || u.uncaptured_transfers || u.iso_errors || u.packet_errors) ? "problems (see details)" : "complete";
        if (p.capture_overruns !== undefined) return p.capture_overruns ? "overruns" : "complete";
        return "–";
      }],
      ["CPU (system)", function (c) { var u = c.a.cpu; return u && u.system_percent != null ? u.system_percent.toFixed(1) + "%" : "–"; }],
      ["Busiest process", function (c) { var u = c.a.cpu; var t = u && u.top && u.top[0]; return t ? esc(t.name) + " " + t.percent_of_one_cpu.toFixed(1) + "%" : "–"; }],
      ["Captured", function (c) { return esc(fmtTime(cap(c).started || c.meta.created)); }]
    ];
    return defs.map(function (d) {
      var cells = cols.map(function (c) { try { return d[1](c); } catch (e) { return "–"; } });
      return "<tr><td>" + d[0] + "</td>" + cells.map(function (x, i) {
        return '<td class="' + (i > 0 && x !== cells[0] ? "diff" : "") + '">' + x + "</td>";
      }).join("") + "</tr>";
    }).join("");
  }

  function runNulls(list) {
    var box = $("#nulls");
    box.innerHTML = list.map(function (p, k) {
      return '<div class="null" id="null-' + k + '"><div class="row spread"><div class="pair">' + esc(itemById(p[1]).name) +
        ' <span class="muted">vs</span> ' + esc(itemById(p[0]).name) + '</div><span class="verdict wait"><span class="spinner"></span> testing</span></div></div>';
    }).join("");
    var k = 0;
    function next() {
      if (k >= list.length || S.view !== "compare") return;
      var p = list[k], idx = k++;
      var key = p[0] + "__" + p[1];
      var got = S.nulls[key] ? Promise.resolve(S.nulls[key]) : api("/api/null", { method: "POST", json: { a: p[0], b: p[1] } });
      got.then(function (r) { S.nulls[key] = r; fillNull(idx, p, r); next(); })
        .catch(function (e) {
          var el = $("#null-" + idx);
          if (el) el.innerHTML += '<div class="alert bad">' + esc(e.message) + "</div>";
          next();
        });
    }
    next();
  }

  function fillNull(idx, p, r) {
    var el = $("#null-" + idx);
    if (!el) return;
    var nums = [];
    if (r.null_db != null) nums.push("difference " + Math.abs(r.null_db).toFixed(1) + " dB below the music");
    if (r.residual_rms_db != null) nums.push("residual " + fmtDb(r.residual_rms_db) + " dBFS rms");
    var html = '<div class="row spread"><div class="pair">' + esc(itemById(p[1]).name) + ' <span class="muted">vs</span> ' +
      esc(itemById(p[0]).name) + '</div><span class="verdict ' + verdictClass(r.verdict) + '">' + esc(r.short) + "</span></div>" +
      "<p><b>" + esc(r.headline) + "</b></p>" + (nums.length ? '<p class="muted">' + esc(nums.join(" · ")) + "</p>" : "") +
      '<ul>' + (r.lines || []).map(function (l) { return "<li>" + esc(l) + "</li>"; }).join("") + "</ul>";
    var plots = r.plots || {};
    if (plots.timeline && plots.timeline.length) html += timelineHtml(plots.timeline);
    html += '<details><summary>Charts and difference file</summary>' +
      (plots.envelope ? '<h3 style="margin-top:10px">Difference over time</h3><canvas class="chart" id="nenv-' + idx + '"></canvas>' : "") +
      (plots.spectrum ? '<h3 style="margin-top:10px">Spectrum of the music and of the difference</h3><canvas class="chart" id="nspec-' + idx + '"></canvas>' +
        '<div class="legend"><span><i style="background:' + PALETTE[0] + '"></i>music</span><span><i style="background:' + PALETTE[1] + '"></i>difference</span></div>' : "") +
      (r.verdict !== "RESAMPLED" && r.verdict !== "CHANNELS" ? '<p><a class="btn small" href="/api/null/' + encodeURIComponent(p[0]) + "/" + encodeURIComponent(p[1]) +
        '/difference">Download the difference (32-bit float WAV)</a></p><p class="muted" style="font-size:.82rem">Digital silence where the files match. Raise the gain a lot to hear anything that is left.</p>' : "") +
      "</details>";
    el.innerHTML = html;
    var det = $("details", el);
    det.addEventListener("toggle", function () {
      if (!det.open) return;
      if (plots.envelope) timeChart($("#nenv-" + idx), [
        { label: "music", color: PALETTE[0], x: plots.envelope.t, y: plots.envelope.signal_db },
        { label: "difference", color: PALETTE[1], x: plots.envelope.t, y: plots.envelope.residual_db }], "dBFS");
      if (plots.spectrum) spectrumChart($("#nspec-" + idx), [
        { label: "music", color: PALETTE[0], x: plots.spectrum.freqs, y: plots.spectrum.signal_db },
        { label: "difference", color: PALETTE[1], x: plots.spectrum.freqs, y: plots.spectrum.residual_db }]);
    });
  }

  function timelineHtml(bands) {
    var end = bands.reduce(function (m, b) { return Math.max(m, b.end); }, 0) || 1;
    var colors = { identical: "var(--ok)", inserted: "var(--warn)", dropped: "var(--warn)", altered: "var(--bad)", replaced: "var(--bad)", repeated: "var(--bad)", differs: "var(--bad)" };
    return '<div class="timeline" title="green: identical, amber: gaps, red: different">' + bands.map(function (b) {
      var w = Math.max(0.4, 100 * (b.end - b.start) / end);
      return '<span style="width:' + w.toFixed(2) + "%;background:" + (colors[b.kind] || "var(--muted)") + '" title="' + esc(b.kind + " " + b.start + "–" + b.end + " s") + '"></span>';
    }).join("") + '</div><p class="muted" style="font-size:.8rem">Timeline of the reference: green = bit-identical, amber = gap, red = different.</p>';
  }

  function bitsHtml(rows) {
    return '<div class="bits">' + rows.map(function (r) {
      return '<div class="bitrow"><span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap"><span class="swatch" style="background:' +
        r.color + '"></span> ' + esc(r.name) + '</span><div class="bitcells">' + r.bits.map(function (f, i) {
        var a = Math.min(1, f * 2);
        return '<span title="bit ' + (31 - i) + ": " + (100 * f).toFixed(1) + '%" style="background:' + (a > 0 ? r.color : "") + ";opacity:" + (a > 0 ? (0.25 + 0.75 * a).toFixed(2) : 1) + '"></span>';
      }).join("") + "</div></div>";
    }).join("") + '<div class="bitaxis"><span>bit 31 (MSB)</span><span>16</span><span>bit 0</span></div></div>';
  }

  function legend(el, series) {
    el.innerHTML = series.map(function (s) { return '<span><i style="background:' + s.color + '"></i>' + esc(s.label) + "</span>"; }).join("");
  }

  // ---------------------------------------------------------------- charts

  function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

  function niceStep(range, target) {
    var raw = range / target, mag = Math.pow(10, Math.floor(Math.log10(raw))), steps = [1, 2, 5, 10];
    for (var i = 0; i < steps.length; i++) if (steps[i] * mag >= raw) return steps[i] * mag;
    return 10 * mag;
  }

  function drawChart(c) {
    var canvas = c.canvas, o = c.opts;
    if (!document.body.contains(canvas)) return;
    var dpr = window.devicePixelRatio || 1, W = canvas.clientWidth, H = canvas.clientHeight;
    if (!W || !H) return;
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    var ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    var L = 44, R = 10, T = 8, B = 22, pw = W - L - R, ph = H - T - B;
    var ys = [];
    o.series.forEach(function (s) { s.y.forEach(function (v) { if (v != null && isFinite(v)) ys.push(v); }); });
    if (!ys.length) { ctx.fillStyle = cssVar("--muted"); ctx.fillText("no data (digital silence)", L + 10, T + 20); return; }
    var ymax = Math.max.apply(null, ys), ymin = Math.min.apply(null, ys);
    ymax = Math.ceil((ymax + 3) / 10) * 10;
    ymin = Math.max(Math.floor(ymin / 10) * 10, ymax - (o.span || 180));
    if (ymax - ymin < 20) ymin = ymax - 20;
    var xmin = o.xMin, xmax = o.xMax;
    var lx = function (x) { return o.xLog ? Math.log10(x) : x; };
    var X = function (x) { return L + (lx(x) - lx(xmin)) / (lx(xmax) - lx(xmin)) * pw; };
    var Y = function (y) { return T + (1 - (y - ymin) / (ymax - ymin)) * ph; };
    ctx.font = "11px system-ui, sans-serif";
    ctx.strokeStyle = cssVar("--line"); ctx.fillStyle = cssVar("--muted"); ctx.lineWidth = 1;
    var ystep = niceStep(ymax - ymin, 5);
    ctx.textAlign = "right"; ctx.textBaseline = "middle";
    for (var y = Math.ceil(ymin / ystep) * ystep; y <= ymax; y += ystep) {
      ctx.beginPath(); ctx.moveTo(L, Y(y)); ctx.lineTo(W - R, Y(y)); ctx.stroke();
      ctx.fillText(String(Math.round(y)), L - 6, Y(y));
    }
    ctx.textAlign = "center"; ctx.textBaseline = "top";
    var xt = o.xLog ? [10, 20, 50, 100, 200, 500, 1e3, 2e3, 5e3, 1e4, 2e4, 5e4, 1e5, 2e5, 5e5] : (function () {
      var st = niceStep(xmax - xmin, Math.max(3, Math.floor(pw / 70))), out = [];
      for (var x = Math.ceil(xmin / st) * st; x <= xmax; x += st) out.push(x);
      return out;
    })();
    xt.forEach(function (x) {
      if (x < xmin || x > xmax) return;
      ctx.beginPath(); ctx.moveTo(X(x), T); ctx.lineTo(X(x), T + ph); ctx.stroke();
      ctx.fillText(o.xFmt(x), X(x), T + ph + 5);
    });
    ctx.save();
    ctx.beginPath(); ctx.rect(L, T, pw, ph); ctx.clip();
    o.series.forEach(function (s) {
      ctx.strokeStyle = s.color; ctx.lineWidth = 1.6; ctx.beginPath();
      var pen = false;
      for (var i = 0; i < s.x.length; i++) {
        var v = s.y[i];
        if (v == null || !isFinite(v) || s.x[i] < xmin || s.x[i] > xmax) { pen = false; continue; }
        var px = X(s.x[i]), py = Y(Math.max(v, ymin));
        if (pen) ctx.lineTo(px, py); else ctx.moveTo(px, py);
        pen = true;
      }
      ctx.stroke();
    });
    ctx.restore();
    ctx.fillStyle = cssVar("--muted"); ctx.textAlign = "left"; ctx.textBaseline = "top";
    if (o.yLabel) ctx.fillText(o.yLabel, L + 4, T + 2);
  }

  function chart(canvas, opts) {
    if (!canvas) return;
    S.charts = S.charts.filter(function (c) { return c.canvas !== canvas && document.body.contains(c.canvas); });
    var c = { canvas: canvas, opts: opts };
    S.charts.push(c);
    drawChart(c);
  }

  function spectrumChart(canvas, series) {
    if (!series.length) return;
    var top = Math.max.apply(null, series.map(function (s) { return s.x[s.x.length - 1]; }));
    chart(canvas, { series: series, xLog: true, xMin: 20, xMax: top, yLabel: "dB", span: 160,
      xFmt: function (x) { return x >= 1000 ? (x / 1000) + "k" : String(x); } });
  }

  function timeChart(canvas, series, unit) {
    if (!series.length) return;
    var top = Math.max.apply(null, series.map(function (s) { return s.x[s.x.length - 1] || 1; }));
    chart(canvas, { series: series, xLog: false, xMin: 0, xMax: top, yLabel: unit, span: 160,
      xFmt: function (x) { return x >= 60 ? Math.floor(x / 60) + ":" + ("0" + Math.round(x % 60)).slice(-2) : x + " s"; } });
  }

  var resizeTimer = null;
  window.addEventListener("resize", function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () { S.charts.forEach(drawChart); }, 150);
  });

  // ---------------------------------------------------------------- polling

  function poll() {
    return api("/api/state").then(function (st) {
      S.state = st;
      renderCaptureLive();
      $("#version").textContent = "SQ-tool " + st.version;
      var cap = st.capture;
      var savedKey = cap ? JSON.stringify((cap.saved || []).map(function (i) { return i.id; })) + cap.state : "";
      if (savedKey !== S.savedSeen) {
        S.savedSeen = savedKey;
        refreshItems();
      }
    }).catch(function () { /* server restarting: try again */ });
  }

  buildCapture();
  refreshItems().catch(function (e) { toast(e.message); });
  poll();
  setInterval(function () {
    var cap = S.state && S.state.capture;
    var busy = cap && (["starting", "waiting", "recording", "stopping"].indexOf(cap.state) >= 0 || cap.saving);
    if (busy || S.view === "capture" || !document.hidden) poll();
  }, 1500);
})();
