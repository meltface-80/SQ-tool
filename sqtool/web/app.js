/* SQ-tool web interface: pick a song, record two players, compare. No dependencies. */
(function () {
  "use strict";

  // Plain, muted colours that stay apart for every reader, colour-blind ones too.
  var COLORS = { source: "#3987e5", a: "#199e70", b: "#d95926" };
  var DIFF = "#e8836b";
  var MUSIC = "#c9c6bd";  // the music beside what remains, on the difference charts
  var ACTIVE = ["starting", "waiting", "recording", "stopping"];
  // The spectrogram colour map (matches sqtool/spectrogram.py; its name keeps pictures in other
  // colours out of the browser's cache).
  var MAP = "inferno";
  var MAP_STEPS = [[0, 0, 4], [22, 11, 57], [66, 10, 104], [106, 23, 110], [147, 38, 103], [188, 55, 84],
    [221, 81, 58], [243, 120, 25], [252, 165, 10], [246, 215, 70], [252, 255, 164]];
  var S = {
    page: null, tid: null, state: null, test: null, rev: null, html: {}, charts: [], timer: null,
    browsePath: load("sq.browse", ""), searchSeq: 0,
    spec: { mode: "signals", scale: load("sq.scale", "log"), floor: load("sq.floor", { signals: -150, diffs: -180 }),
      matched: false, t0: 0, t1: 0, data: "", panels: [], info: {}, width: 0 }
  };
  var main = document.getElementById("main");

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
  function setHtml(el, html) {
    // Only touch the DOM when something changed: keeps taps, focus and open <details> intact.
    if (!el || el.__html === html) return false;
    el.__html = html;
    el.innerHTML = html;
    return true;
  }
  function hash(s) {
    var h = 5381;
    for (var i = 0; i < s.length; i++) h = ((h * 33) ^ s.charCodeAt(i)) >>> 0;
    return h.toString(36);
  }

  function api(path, opts) {
    opts = opts || {};
    var init = { method: opts.method || "GET", headers: {} };
    if (opts.json !== undefined) { init.body = JSON.stringify(opts.json); init.headers["Content-Type"] = "application/json"; }
    return fetch(path, init).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
        return data;
      });
    });
  }

  var toastTimer = null;
  function toast(msg) {
    var t = $("#toast");
    t.textContent = msg; t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.hidden = true; }, 5000);
  }

  function fmtRate(r) { if (!r) return "–"; var k = r / 1000; return (k % 1 === 0 ? k.toFixed(0) : k.toFixed(1)) + " kHz"; }
  function fmtClock(s) {
    s = Math.max(0, Math.floor(s || 0));
    var m = Math.floor(s / 60), sec = s % 60;
    return m + ":" + (sec < 10 ? "0" : "") + sec;
  }
  function fmtAt(s, dec) {
    // m:ss with `dec` decimals, rounding carried into the minutes correctly.
    var scale = Math.pow(10, dec), units = Math.round(Math.max(0, s) * scale), perMin = 60 * scale;
    var m = Math.floor(units / perMin), rest = (units - m * perMin) / scale;
    var txt = rest.toFixed(dec);
    return m + ":" + (rest < 10 ? "0" : "") + txt;
  }
  function fmtSecs(s) { return s == null ? "–" : s < 60 ? s.toFixed(s < 10 ? 3 : 2) + " s" : fmtAt(s, 2); }
  function fmtTime(iso) {
    var d = iso ? new Date(iso) : null;
    if (!d || isNaN(d.getTime())) return iso ? String(iso).replace("T", " ").slice(0, 16) : "–";
    return d.toLocaleString([], { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }
  function fmtDb(v, digits) { return v == null ? "−∞" : (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(digits == null ? 1 : digits); }
  function fmtNum(n) { return n == null ? "–" : Number(n).toLocaleString(); }
  function fmtBytes(n) {
    if (n == null) return "–";
    var u = ["B", "KB", "MB", "GB", "TB"], i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return n.toFixed(i ? 1 : 0) + " " + u[i];
  }
  function fmtHz(f) { return f >= 1000 ? (f / 1000 >= 10 ? (f / 1000).toFixed(1) : (f / 1000).toFixed(2)) + " kHz" : f.toFixed(0) + " Hz"; }
  function fpColor(fp) { return fp ? "hsl(" + (parseInt(fp.slice(0, 6), 16) % 360) + " 32% 56%)" : "transparent"; }
  function swatch(color) { return '<span class="swatch" style="background:' + color + '"></span>'; }
  function players() { var s = S.state && S.state.settings; return (s && s.players) || { a: "Roon", b: "Mandarin" }; }
  function recActive(rec) { return rec && ACTIVE.indexOf(rec.state) >= 0; }
  // Recording the music itself (not just a player's open, silent output).
  function hearing(rec) { return !!(rec && rec.state === "recording" && rec.take && rec.take.music_seconds != null); }
  function whoName(t, w) { return w === "source" ? "Original file" : t.players[w]; }
  function plural(n, one, many) { return n + " " + (n === 1 ? one : many); }

  // ---------------------------------------------------------------- pages

  function route() {
    var h = location.hash || "#/";
    var m = h.match(/^#\/test\/([A-Za-z0-9._-]+)/);
    closeSheet();
    if (m) showTest(m[1]);
    else if (h.indexOf("#/new") === 0) showNew();
    else showHome();
  }
  window.addEventListener("hashchange", function () { route(); window.scrollTo(0, 0); });

  function leavePage() {
    S.charts = [];
    S.html = {};
    S.test = null;
    S.rev = null;
  }

  // ---------------------------------------------------------------- home

  function showHome() {
    leavePage();
    S.page = "home"; S.tid = null;
    var p = players();
    main.innerHTML =
      '<div id="setup"></div>' +
      '<section class="card hero"><h1>Do ' + esc(p.a) + " and " + esc(p.b) + " send your DAC exactly what is in the file?</h1>" +
      "<p>SQ-tool records exactly what each player sends to its output and compares it, sample by sample, with the original file and with each other.</p>" +
      "<ol><li>Choose a song from your music folder.</li>" +
      "<li>Play it in <b>" + esc(p.a) + "</b> to the <b>Loopback</b> output. SQ-tool records it.</li>" +
      "<li>Play it in <b>" + esc(p.b) + "</b> the same way.</li>" +
      "<li>See whether each one is bit-perfect, and whether both would give your DAC the same data, with spectrograms and charts.</li></ol>" +
      '<a class="btn primary big" href="#/new">Start a new test</a></section>' +
      '<section class="card"><h2>Previous tests</h2><div id="tests" class="tests"><p class="muted">Loading…</p></div></section>';
    renderSetup();
    api("/api/tests").then(function (list) {
      if (S.page !== "home") return;
      $("#tests").innerHTML = list.length ? list.map(testItem).join("") : '<p class="empty">No tests yet.</p>';
    }).catch(function (e) { if (S.page === "home") $("#tests").innerHTML = '<div class="alert bad">' + esc(e.message) + "</div>"; });
  }

  function testItem(t) {
    var chips = [];
    ["a", "b"].forEach(function (k) {
      var r = t.results[k];
      if (r && r.verdict) chips.push('<span class="chip ' + verdictClass(r.verdict) + '">' + esc(t.players[k]) + ": " + esc(badgeText(r.verdict, k)) + "</span>");
      else if (t.captures[k]) chips.push('<span class="chip">' + esc(t.players[k]) + ": recorded</span>");
    });
    var ab = t.results.ab;
    if (ab && ab.verdict) chips.push('<span class="chip ' + verdictClass(ab.verdict) + '">' + esc(badgeText(ab.verdict, "ab")) + "</span>");
    var src = t.source || {};
    return '<a class="test-item" href="#/test/' + encodeURIComponent(t.id) + '"><div><div class="name">' + esc(t.title) + "</div>" +
      '<div class="muted small">' + (t.artist ? esc(t.artist) + " · " : "") + esc(fmtTime(t.created)) +
      (src.rate ? " · " + fmtRate(src.rate) + (src.bits ? "/" + src.bits + "-bit" : "") : "") + "</div>" +
      '<div class="chips">' + (chips.join("") || '<span class="chip">no recordings yet</span>') + '</div></div><span class="go">›</span></a>';
  }

  function renderSetup() {
    var el = $("#setup");
    if (!el || !S.state) return;
    var cap = S.state.capture || {}, html = "", quiet = S.page === "test";
    if ((cap.problems || []).length) {
      html = '<div class="alert warn">' + cap.problems.map(function (p) { return "<p>" + esc(p) + "</p>"; }).join("");
      if (!cap.loopback && cap.kind !== "usb") {
        html += cap.can_load_loopback
          ? '<p><button class="btn small" data-act="load-loopback">Load the Loopback driver now</button></p>'
          : "<p>On the server, run:</p><pre class=\"cmd\">sudo modprobe snd-aloop</pre>";
        html += "<p>Then restart Roon Server (and your other player) so they list the new Loopback output.</p>";
      }
      html += "</div>";
    } else if (cap.use && !quiet) {
      var lp = cap.loopback, now = "";
      var playing = cap.use.indexOf("loopback") === 0 ? lp && lp.playing : null;
      if (playing) now = " Playing now: " + esc(playing.format || "") + " · " + fmtRate(playing.rate) + (playing.player ? " from " + esc(playing.player) : "") + ".";
      var where = cap.use.indexOf("loopback") === 0
        ? "Players play to the <b>Loopback</b> output (" + esc(lp.play_to) + ")."
        : "Recording from the USB DAC (" + esc((cap.dacs.filter(function (d) { return d.id === cap.use; })[0] || {}).name || cap.use) + ").";
      html = '<div class="card setup"><span class="dot on"></span><span>Ready. ' + where + now +
        ' <button class="linkbtn" data-act="help">How to set up the players</button></span></div>';
    }
    setHtml(el, html);
  }

  // ---------------------------------------------------------------- choosing the song

  function showNew() {
    leavePage();
    S.page = "new"; S.tid = null;
    var p = players();
    main.innerHTML =
      '<div id="setup"></div>' +
      '<section class="card"><h1>Choose the song</h1><p class="muted">The same file ' + esc(p.a) + " and " + esc(p.b) +
      " will play. SQ-tool compares what they send with it.</p>" +
      '<input type="search" id="q" placeholder="Search your music folder" autocomplete="off" enterkeyhint="search" aria-label="Search your music folder">' +
      '<div id="browser"><p class="muted">Loading…</p></div></section>' +
      '<section class="card stack"><h2>Or upload the file</h2><p class="muted" style="margin:0">From this phone or tablet: FLAC, WAV or AIFF.</p>' +
      '<input type="file" id="upload" accept=".flac,.wav,.wave,.aif,.aiff,.aifc,audio/*" hidden>' +
      '<div><button class="btn" data-act="upload">Choose a file…</button></div><div id="upload-status"></div></section>';
    renderSetup();
    browse(S.browsePath);
    var timer = null;
    $("#q").addEventListener("input", function (e) {
      clearTimeout(timer);
      var q = e.target.value.trim();
      timer = setTimeout(function () {
        if (S.page === "new") q ? search(q) : browse(S.browsePath);
      }, 300);
    });
    $("#upload").addEventListener("change", function (e) { if (e.target.files[0]) upload(e.target.files[0]); });
  }

  function songButton(path, name, meta, ok) {
    if (ok === false) {
      return '<button class="file song off" disabled title="SQ-tool reads FLAC, WAV and AIFF files"><span class="ico">♪</span><span class="fname">' +
        esc(name) + '</span><span class="fmeta">can\'t be read here</span></button>';
    }
    return '<button class="file song" data-song="' + esc(path) + '"><span class="ico">♪</span><span class="fname">' + esc(name) +
      '</span><span class="fmeta">' + esc(meta || "") + "</span></button>";
  }

  function browse(path) {
    var seq = ++S.searchSeq;
    api("/api/browse?path=" + encodeURIComponent(path)).then(function (d) {
      if (seq !== S.searchSeq || S.page !== "new" || ($("#q") && $("#q").value.trim())) return;
      if (!d.available) {
        $("#browser").innerHTML = '<div class="alert warn" style="margin-top:10px"><p>Your music folder is not connected to SQ-tool.</p>' +
          "<p>Add it to the <code>docker run</code> command, for example <code>-v /path/to/your/music:/music:ro</code>, and start the container again. Or upload the file below.</p></div>";
        return;
      }
      S.browsePath = d.path;
      save("sq.browse", d.path);
      var parts = d.path ? d.path.split("/") : [];
      var crumbs = '<div class="crumbs"><button data-dir="">Music</button>' + parts.map(function (part, i) {
        return '<span>›</span><button data-dir="' + esc(parts.slice(0, i + 1).join("/")) + '">' + esc(part) + "</button>";
      }).join("") + "</div>";
      var rows = d.dirs.map(function (name) {
        return '<button class="file" data-dir="' + esc(d.path ? d.path + "/" + name : name) + '"><span class="ico">▸</span><span class="fname">' + esc(name) + "</span></button>";
      }).concat(d.files.map(function (f) {
        return songButton(d.path ? d.path + "/" + f.name : f.name, f.name, fmtBytes(f.size), f.ok);
      }));
      $("#browser").innerHTML = crumbs + '<div class="files">' + (rows.join("") || '<p class="empty">No folders or songs here.</p>') + "</div>";
    }).catch(function (e) {
      if (S.page !== "new") return;
      if (path) { S.browsePath = ""; browse(""); return; }
      $("#browser").innerHTML = '<div class="alert bad">' + esc(e.message) + "</div>";
    });
  }

  function search(q) {
    var seq = ++S.searchSeq, box = $("#browser");
    if (!box) return;
    box.innerHTML = '<p class="muted"><span class="spinner"></span> Searching…</p>';
    api("/api/search?q=" + encodeURIComponent(q)).then(function (list) {
      if (seq !== S.searchSeq || !document.body.contains(box)) return;
      box.innerHTML = '<div class="files" style="margin-top:8px">' + (list.length ? list.map(function (f) {
        return songButton(f.path, f.name, f.folder, f.ok);
      }).join("") : '<p class="empty">Nothing found for “' + esc(q) + "”.</p>") + "</div>";
    }).catch(function (e) { if (document.body.contains(box)) box.innerHTML = '<div class="alert bad">' + esc(e.message) + "</div>"; });
  }

  function createTest(path) {
    $all("[data-song]").forEach(function (b) { b.disabled = true; });
    api("/api/tests", { method: "POST", json: { path: path } }).then(function (r) {
      location.hash = "#/test/" + encodeURIComponent(r.id);
    }).catch(function (e) {
      toast(e.message);
      $all("[data-song]").forEach(function (b) { b.disabled = false; });
    });
  }

  function upload(file) {
    var box = $("#upload-status");
    box.innerHTML = '<p class="small">Uploading ' + esc(file.name) + ' (' + fmtBytes(file.size) + ')…</p><div class="upload-progress"><span></span></div>';
    var xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/tests/upload?filename=" + encodeURIComponent(file.name));
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = function (e) { if (e.lengthComputable) $(".upload-progress span", box).style.width = (100 * e.loaded / e.total).toFixed(1) + "%"; };
    xhr.onload = function () {
      var data = {};
      try { data = JSON.parse(xhr.responseText); } catch (err) { /* not JSON */ }
      if (xhr.status === 200 && data.id) location.hash = "#/test/" + encodeURIComponent(data.id);
      else box.innerHTML = '<div class="alert bad">' + esc(data.error || "Upload failed (HTTP " + xhr.status + ")") + "</div>";
    };
    xhr.onerror = function () { box.innerHTML = '<div class="alert bad">Upload failed: the connection was lost.</div>'; };
    xhr.send(file);
  }

  // ---------------------------------------------------------------- a test

  function showTest(tid) {
    var same = S.page === "test" && S.tid === tid;
    leavePage();
    if (!same) { S.spec.t0 = 0; S.spec.t1 = 0; }
    S.spec.data = "";
    S.page = "test"; S.tid = tid;
    main.innerHTML =
      '<div id="setup"></div>' +
      '<section class="card" id="t-head"><span class="spinner"></span> Loading…</section>' +
      '<section class="card" id="t-steps" hidden></section>' +
      '<section class="card" id="t-results" hidden></section>' +
      '<section class="card" id="t-spec" hidden></section>' +
      '<section class="card" id="t-charts" hidden></section>' +
      '<section class="card" id="t-details" hidden></section>' +
      '<section class="card" id="t-downloads" hidden></section>';
    loadTest();
  }

  function loadTest() {
    var tid = S.tid;
    return api("/api/tests/" + encodeURIComponent(tid)).then(function (t) {
      if (S.page !== "test" || S.tid !== tid) return;
      S.test = t; S.rev = t.rev;
      renderTest();
    }).catch(function (e) {
      if (S.page !== "test" || S.tid !== tid) return;
      $("#t-head").innerHTML = '<div class="alert bad">' + esc(e.message === "not found" ? "This test does not exist (any more)." : e.message) +
        '</div><p><a class="btn" href="#/">Back to the start</a></p>';
    });
  }

  function renderTest() {
    renderHead(); renderSteps(); renderResults(); renderSpec(); renderCharts(); renderDetails(); renderDownloads();
  }

  function renderHead() {
    var t = S.test, src = t.source, a = t.analysis.source || {};
    var facts = [];
    if (src.state === "ready") {
      facts.push(esc(src.label));
      facts.push(fmtRate(src.rate));
      if (src.resolution) facts.push(src.resolution + " bits in use");
      if (src.channels !== 2) facts.push(src.channels + " channels");
      facts.push(fmtClock(src.duration));
      var peak = (a.channel_stats || []).reduce(function (m, c) { return c.peak_db != null && c.peak_db > m ? c.peak_db : m; }, -Infinity);
      if (isFinite(peak)) facts.push("peak " + fmtDb(peak) + " dBFS");
    }
    setHtml($("#t-head"), '<div class="song-head"><div><h1>' + esc(t.title) + "</h1>" +
      '<div class="sub">' + esc([t.artist, t.album].filter(Boolean).join(" · ") || src.filename) + "</div>" +
      '<div class="facts">' + facts.map(function (f) { return '<span class="chip">' + f + "</span>"; }).join("") + "</div></div>" +
      '<button class="btn small" data-act="test-menu" aria-label="Test options">⋯</button></div>');
  }

  // -- steps: the song, then each player

  function renderSteps() {
    var t = S.test, el = $("#t-steps");
    if (!t || !el) return;
    el.hidden = false;
    var st = S.state || {}, rec = st.recorder, cap = st.capture || {};
    var busyElsewhere = recActive(rec) && rec.test !== t.id;
    var src = t.source, html = '<div class="steps">';
    var srcDetail = src.state === "ready" ? "Analysed: " + esc(src.label) + " · " + fmtRate(src.rate) + " · " + fmtClock(src.duration)
      : src.state === "error" ? '<span style="color:var(--bad)">' + esc(src.message) + "</span>"
        : '<span class="spinner"></span> Reading and analysing the song…';
    html += step(1, src.state === "ready" ? "done" : src.state === "error" ? "" : "now", "The song", srcDetail, "");
    var next = !t.captures.a ? "a" : !t.captures.b ? "b" : null;
    ["a", "b"].forEach(function (slot, i) { html += playerStep(i + 2, slot, t, rec, cap, busyElsewhere, next === slot); });
    html += "</div>";
    if (busyElsewhere) {
      html += '<div class="alert info" style="margin-top:12px">Another test is recording right now. <a href="#/test/' +
        encodeURIComponent(rec.test) + '">Go to it</a></div>';
    }
    setHtml(el, html);
    updateLive(rec, t);
    $("#rec-badge").hidden = !hearing(rec);
  }

  function step(n, cls, what, detail, act, extra) {
    return '<div class="step ' + cls + '"><div class="num">' + (cls === "done" ? "✓" : n) + '</div><div><div class="what">' + what + "</div>" +
      (detail ? '<div class="detail">' + detail + "</div>" : "") + (act ? '<div class="act">' + act + "</div>" : "") + (extra || "") + "</div></div>";
  }

  function playerStep(n, slot, t, rec, capInfo, busyElsewhere, isNext) {
    var name = t.players[slot], cap = t.captures[slot], res = t.results[slot];
    var who = '<span class="row" style="gap:6px">' + swatch(COLORS[slot]) + esc(name) + "</span>";
    var here = rec && rec.test === t.id && rec.slot === slot;
    if (here && recActive(rec)) {
      return step(n, hearing(rec) ? "recording" : "now", who, "", "", liveBox(rec, t, name));
    }
    var songReady = t.source.state === "ready";
    var canRecord = !recActive(rec) && capInfo.use && songReady;
    var recBtn = function (label, big) {
      return '<button class="btn ' + (big ? "rec big" : "small") + '" data-act="record" data-slot="' + slot + '"' + (canRecord ? "" : " disabled") + ">" +
        (big ? '<span class="recdot"></span>' : "") + esc(label) + "</button>";
    };
    var failed = here && !rec.saved && rec.error ? '<div class="alert bad" style="margin-top:8px">' + esc(rec.error) + "</div>" : "";
    if (cap && cap.state === "analysing") return step(n, "now", who, '<span class="spinner"></span> Analysing the recording…', "");
    if (cap && cap.state === "error") {
      return step(n, "", who, '<span style="color:var(--bad)">' + esc(cap.message || "the recording could not be analysed") + "</span>", recBtn("Record " + name + " again", false));
    }
    if (cap && cap.state === "ready") {
      var badge = !res ? '<span class="badge wait">' + (t.source.state === "ready" ? '<span class="spinner"></span> comparing' : "waiting for the song") + "</span>"
        : res.state === "comparing" ? '<span class="badge wait"><span class="spinner"></span> comparing</span>'
          : res.state === "error" ? '<span class="badge bad">error</span>'
            : '<span class="badge ' + verdictClass(res.verdict) + '">' + esc(badgeText(res.verdict, slot)) + "</span>";
      var detail = "Recorded " + fmtClock(cap.seconds) + " of " + esc(cap.format || "audio") + " · " + fmtRate(cap.rate) +
        (cap.player ? " · sent by " + esc(cap.player) : "") + (cap.recorded ? " · " + esc(fmtTime(cap.recorded)) : "");
      return step(n, "done", who + badge, detail, recBtn("Record again", false));
    }
    var toUsb = capInfo.use && capInfo.use.indexOf("usb") === 0;
    var hint = !isNext ? "" : !songReady ? "You can record as soon as the song has been analysed."
      : "Press Record, then play the song in " + esc(name) + (toUsb ? " to your USB DAC." : " to the Loopback output.");
    return step(n, isNext ? "now" : "", who, hint + failed, recBtn("Record " + name, isNext));
  }

  function liveBox(rec, t, name) {
    if (hearing(rec)) {
      return '<div class="live-box rec" aria-live="polite"><div class="row spread"><span><span class="dot rec"></span><b>Recording ' + esc(name) +
        '</b></span></div><div class="clock" id="live-clock"></div>' +
        '<div class="progress" id="live-bar"' + (rec.expected_seconds ? "" : " hidden") + '><span></span></div>' +
        '<div class="small muted" id="live-fmt"></div>' +
        '<div class="small muted">It stops by itself at the song\'s last sample, even if ' + esc(name) + " goes on to the next track.</div>" +
        '<div class="row"><button class="btn rec" data-act="stop">Stop now</button></div></div>';
    }
    if (rec.state === "stopping") return '<div class="live-box"><div class="row"><span class="spinner"></span> Finishing the recording…</div></div>';
    var cap = (S.state && S.state.capture) || {}, lp = cap.loopback;
    var where = cap.use && cap.use.indexOf("usb") === 0 ? "to your USB DAC" : "to the <b>Loopback</b> output" + (lp ? " (" + esc(lp.play_to) + ")" : "");
    // A player can hold its output open and silent before the song starts (Squeezelite does).
    var open = rec.state === "recording" ? '<div class="small muted">' + esc(name) + "'s output is open and silent: SQ-tool is listening.</div>" : "";
    if (rec.ignoring && rec.state !== "recording") {
      open += '<div class="small muted">' + esc(rec.ignoring) + " is still playing to the Loopback (it went on to its next track). SQ-tool leaves that alone and waits for " + esc(name) + ".</div>";
    }
    return '<div class="live-box" aria-live="polite"><div class="row"><span class="spinner"></span><b>Waiting for ' + esc(name) + "…</b></div>" +
      "<div>Now play <b>“" + esc(t.title) + "”</b> in " + esc(name) + ", from the beginning, " + where + ". Recording starts by itself.</div>" + open +
      '<div class="row"><button class="btn" data-act="stop">Cancel</button><button class="linkbtn" data-act="help">How do I set up ' + esc(name) + "?</button></div></div>";
  }

  function updateLive(rec, t) {
    // The clock and progress change every second: update them in place, not by re-rendering.
    var clock = $("#live-clock");
    if (!clock || !hearing(rec)) return;
    // Where in the song the player is, once SQ-tool has recognised it; until then, time since the music began.
    var take = rec.take, secs = take.song_seconds != null ? Math.max(0, take.song_seconds) : take.music_seconds;
    var exp = rec.expected_seconds;
    clock.innerHTML = fmtClock(secs) + (exp ? " <small>/ " + fmtClock(exp) + "</small>" : "");
    var bar = $("#live-bar span");
    if (bar && exp) bar.style.width = Math.min(100, 100 * secs / exp).toFixed(1) + "%";
    $("#live-fmt").textContent = (take.format || "") + " · " + fmtRate(take.rate) + " · " + (take.channels || "?") + " channels" +
      (take.player && take.player.name ? " · sent by " + take.player.name : "");
  }

  // -- verdicts

  function verdictClass(v) {
    if (v === "IDENTICAL" || v === "PARTIAL") return "good";
    if (v === "GAPS" || v === "NO SIGNAL") return "meh";
    return "bad";
  }

  function badgeText(v, key) {
    var pair = key === "ab";
    return ({
      IDENTICAL: pair ? "SAME DATA" : "BIT-PERFECT",
      PARTIAL: pair ? "SAME DATA" : "BIT-PERFECT",
      GAPS: "DROPOUTS",
      ALTERED: "ALTERED",
      DIFFERENT: pair ? "DIFFERENT DATA" : "NOT BIT-PERFECT",
      RESAMPLED: pair ? "DIFFERENT RATES" : "RESAMPLED",
      CHANNELS: "CHANNELS DIFFER",
      "NO MATCH": pair ? "NO MATCH" : "SONG NOT FOUND",
      "NO SIGNAL": "NO SIGNAL"
    })[v] || v;
  }

  function gainText(g) {
    if (!g || !g.length) return null;
    var same = g.every(function (x) { return x != null && Math.abs(x - g[0]) < 0.005; });
    return same ? fmtDb(g[0], 2) + " dB" : g.map(function (x) { return fmtDb(x, 2); }).join(" / ") + " dB";
  }

  function plainText(t, key, r) {
    var p = t.players, P = esc(key === "ab" ? "" : p[key]), A = esc(p.a), B = esc(p.b);
    var v = r.verdict, out;
    if (key !== "ab") {
      out = {
        IDENTICAL: "Every sample " + P + " sent is exactly the sample in the file.",
        PARTIAL: "Every sample " + P + " sent matches the file, but part of the song is missing from the recording.",
        GAPS: "The samples are unchanged, but the stream has " + plural(r.events || 0, "gap or jump", "gaps or jumps") + " (dropouts).",
        ALTERED: "Parts of the stream differ from the file.",
        DIFFERENT: P + " changed the samples.",
        RESAMPLED: P + " sent " + fmtRate((t.captures[key] || {}).rate) + " instead of the file's " + fmtRate(t.source.rate) + ": it resampled the music.",
        CHANNELS: P + " sent a different number of channels.",
        "NO MATCH": "The song could not be found in what " + P + " sent. Did it play this file?",
        "NO SIGNAL": "The file is digital silence."
      }[v] || esc(r.headline);
      if (v === "PARTIAL" && r.missing_start_seconds > 0 && !r.missing_start_silent) {
        out += " The first " + fmtSecs(r.missing_start_seconds) + " was not recorded: recording starts a moment after " + P + " starts playing.";
      }
    } else {
      var fa = (t.captures.a || {}).format, fb = (t.captures.b || {}).format;
      out = {
        IDENTICAL: A + " and " + B + " send exactly the same samples: your DAC would convert identical data.",
        PARTIAL: "Where both recordings overlap, " + A + " and " + B + " send exactly the same samples.",
        GAPS: "The samples are the same, but one of the streams has dropouts.",
        ALTERED: "Parts of the two streams differ.",
        DIFFERENT: A + " and " + B + " send different samples: your DAC would convert different data.",
        RESAMPLED: "They send different sample rates (" + fmtRate((t.captures.a || {}).rate) + " and " + fmtRate((t.captures.b || {}).rate) + ").",
        CHANNELS: "They send different numbers of channels.",
        "NO MATCH": "The two recordings could not be lined up."
      }[v] || esc(r.headline);
      if ((v === "IDENTICAL" || v === "PARTIAL") && fa && fb && fa !== fb) {
        out += " (They package the samples differently, " + esc(fa) + " and " + esc(fb) + ", but the values are the same.)";
      }
    }
    return out;
  }

  function verdictCard(t, key) {
    var r = t.results[key], p = t.players;
    var head = key === "ab"
      ? '<div class="q">' + swatch(COLORS.a) + esc(p.a) + " vs " + swatch(COLORS.b) + esc(p.b) + "</div>"
      : '<div class="q">' + swatch(COLORS[key]) + esc(p[key]) + " vs the original file</div>";
    if (!r) {
      var need = (key === "ab" ? ["a", "b"] : [key]).filter(function (k) { return !t.captures[k]; }).map(function (k) { return esc(p[k]); });
      var wait = need.length ? "Record " + need.join(" and ") + " to see this" : t.source.state !== "ready" ? "Waiting for the song" : '<span class="spinner"></span> Comparing…';
      return '<div class="verdict-card">' + head + '<span class="badge wait">' + wait + "</span></div>";
    }
    if (r.state === "comparing") return '<div class="verdict-card">' + head + '<span class="badge wait"><span class="spinner"></span> Comparing…</span></div>';
    if (r.state === "error") return '<div class="verdict-card bad">' + head + '<span class="badge bad">Error</span><p class="small">' + esc(r.message) + "</p></div>";
    var cls = verdictClass(r.verdict), nums = [];
    var g = gainText(r.gain_db);
    if (g && r.verdict !== "IDENTICAL") nums.push("level " + g);
    if (r.null_db != null && r.residual_rms_db != null) {
      nums.push("after matching the level, what remains is " + Math.abs(r.null_db).toFixed(1) + " dB below the music (" + fmtDb(r.residual_rms_db) + " dBFS rms)");
    }
    var tl = r.plots && r.plots.timeline;
    return '<div class="verdict-card ' + cls + '">' + head + '<span class="badge ' + cls + '">' + esc(badgeText(r.verdict, key)) + "</span>" +
      "<div>" + plainText(t, key, r) + "</div>" +
      (nums.length ? '<div class="small muted">' + esc(nums.join(" · ")) + "</div>" : "") +
      (tl && tl.length && r.verdict !== "IDENTICAL" ? timelineHtml(tl, key === "ab" ? (t.captures.a || {}).seconds : t.source.duration) : "") +
      ((r.lines || []).length ? "<details><summary>Details</summary><ul>" + r.lines.map(function (l) { return "<li>" + esc(l) + "</li>"; }).join("") + "</ul></details>" : "") +
      "</div>";
  }

  function timelineHtml(bands, total) {
    var end = Math.max(total || 0, bands.reduce(function (m, b) { return Math.max(m, b.end); }, 0)) || 1;
    var colors = { identical: "var(--ok)", inserted: "var(--warn)", dropped: "var(--warn)", repeated: "var(--warn)", ending: "var(--muted)" };
    return '<div class="timeline">' + bands.map(function (b) {
      return '<span style="left:' + (100 * b.start / end).toFixed(3) + "%;width:" + (100 * (b.end - b.start) / end).toFixed(3) +
        "%;background:" + (colors[b.kind] || "var(--bad)") + '" title="' + esc(b.kind + " at " + fmtSecs(b.start)) + '"></span>';
    }).join("") + '</div><div class="tl-legend">Where the samples match over the song: green = identical, amber = dropout or jump, red = different.</div>';
  }

  function summaryText(t) {
    var p = t.players, r = t.results;
    var ready = function (x) { return x && x.state === "ready"; };
    var good = function (x) { return ready(x) && (x.verdict === "IDENTICAL" || x.verdict === "PARTIAL"); };
    var gaps = function (x) { return ready(x) && x.verdict === "GAPS"; };
    var says = function (k) {  // what one player did with the file
      return "<b>" + esc(p[k]) + "</b> " + (good(r[k]) ? "is bit-perfect" : gaps(r[k])
        ? "sent the file's samples unchanged, but its stream had dropouts" : "is not bit-perfect");
    };
    if (ready(r.a) && ready(r.b)) {
      if (good(r.a) && good(r.b)) {
        return "Both players are bit-perfect: <b>" + esc(p.a) + "</b> and <b>" + esc(p.b) +
          "</b> send your DAC exactly the samples in the file, so it converts identical data from both.";
      }
      var both = !ready(r.ab) ? "" : good(r.ab) ? " Yet both send the same samples as each other."
        : gaps(r.ab) ? " Apart from the dropouts, both send the same samples."
          : " Your DAC would receive different data from the two.";
      return says("a") + "; " + says("b") + "." + both;
    }
    var one = ready(r.a) ? "a" : ready(r.b) ? "b" : null;
    if (!one) return "";
    var other = one === "a" ? "b" : "a";
    return says(one) + "." + (t.captures[other] ? "" : " Now record <b>" + esc(p[other]) + "</b>.");
  }

  function renderResults() {
    var t = S.test, el = $("#t-results");
    el.hidden = !Object.keys(t.captures).length;
    if (el.hidden) return;
    var summary = summaryText(t);
    setHtml(el, "<h2>Results</h2>" + (summary ? '<p class="summary">' + summary + "</p>" : "") +
      '<div class="verdicts">' + ["a", "b", "ab"].map(function (k) { return verdictCard(t, k); }).join("") + "</div>");
  }

  // ---------------------------------------------------------------- spectrograms

  function specPanels(t) {
    var p = t.players, mode = S.spec.mode, out = [];
    var capReady = function (k) { return t.captures[k] && t.captures[k].state === "ready"; };
    var resReady = function (k) { return t.results[k] && t.results[k].state === "ready"; };
    if (mode === "signals") {
      out.push({ which: "source", label: "Original file", color: COLORS.source, ok: true });
      ["a", "b"].forEach(function (k) {
        out.push({ which: k, label: p[k], color: COLORS[k], ok: capReady(k), wait: "Record " + p[k] + " to see what it sent", marks: k });
      });
    } else {
      out.push({ which: "diff-a", label: p.a + " − original", color: COLORS.a, ok: resReady("a"), wait: "Record " + p.a + " to see this difference", marks: "a" });
      out.push({ which: "diff-b", label: p.b + " − original", color: COLORS.b, ok: resReady("b"), wait: "Record " + p.b + " to see this difference", marks: "b" });
      out.push({ which: "diff-ab", label: p.b + " − " + p.a, color: DIFF, ok: resReady("ab"), wait: "Record both players to see this difference", marks: "ab" });
    }
    return out;
  }

  function specData(t) {
    // Everything the images depend on: when it changes, they are fetched again.
    var parts = [t.source.fingerprint];
    ["a", "b"].forEach(function (k) { var c = t.captures[k] || {}; parts.push(c.state, c.recorded, c.fingerprint, c.seconds); });
    ["a", "b", "ab"].forEach(function (k) { var r = t.results[k] || {}; parts.push(r.state, r.created, JSON.stringify(r.alignment || null)); });
    return hash(JSON.stringify(parts));
  }

  function renderSpec() {
    var t = S.test, el = $("#t-spec");
    el.hidden = t.source.state !== "ready";
    if (el.hidden) return;
    var sp = S.spec, dur = t.source.duration;
    if (!sp.t1 || sp.t1 > dur) { sp.t0 = 0; sp.t1 = dur; }
    if (!el.__built) {
      el.__built = true;
      el.innerHTML = '<div class="card-head"><h2>Spectrogram</h2><div class="seg" role="group" aria-label="What to show">' +
        '<button data-spec-mode="signals">What each one sent</button><button data-spec-mode="diffs">Differences</button></div></div>' +
        '<div class="toolbar">' +
        '<div class="seg" role="group" aria-label="Frequency scale"><button data-spec-scale="log">Log</button><button data-spec-scale="linear">Linear</button></div>' +
        '<select id="spec-floor" aria-label="Lowest level shown">' + [-100, -120, -150, -180, -210].map(function (v) {
          return '<option value="' + v + '">down to ' + v + " dB</option>";
        }).join("") + "</select>" +
        '<label class="check" id="spec-matched-wrap"><input type="checkbox" id="spec-matched"> Match levels first</label></div>' +
        '<div class="zoombar"><button class="btn small" data-zoom="all" title="Whole song">Whole song</button>' +
        '<button class="btn small" data-zoom="out" aria-label="Zoom out" title="Zoom out">−</button>' +
        '<button class="btn small" data-zoom="in" aria-label="Zoom in" title="Zoom in">+</button>' +
        '<button class="btn small" data-zoom="left" aria-label="Earlier" title="Earlier">◀</button>' +
        '<button class="btn small" data-zoom="right" aria-label="Later" title="Later">▶</button>' +
        '<span class="range" id="spec-range"></span></div>' +
        '<div class="spec-stack" id="spec-stack"></div><div class="taxis" id="spec-taxis"></div>' +
        '<div class="colorbar"><span id="cb-low"></span><i id="cb-grad"></i><span>0 dB</span></div>' +
        '<p class="spec-note" id="spec-note"></p>';
      $("#spec-floor").addEventListener("change", function (e) {
        sp.floor[sp.mode] = Number(e.target.value); save("sq.floor", sp.floor); loadSpec();
      });
      $("#spec-matched").addEventListener("change", function (e) { sp.matched = e.target.checked; loadSpec(); });
      $("#cb-grad").style.background = "linear-gradient(to right," + MAP_STEPS.map(function (c, i) {
        return "rgb(" + c.join(",") + ") " + (10 * i) + "%";
      }).join(",") + ")";
      bindSpecPointer($("#spec-stack"));
    }
    var data = specData(t), panels = specPanels(t);
    var key = data + "|" + sp.mode + "|" + panels.map(function (p) { return p.which + p.ok + p.label; }).join(",");
    if (key !== sp.data) {
      sp.data = key;
      buildPanels(panels);
    }
    loadSpec();
  }

  function buildPanels(panels) {
    S.spec.panels = panels;
    $("#spec-stack").innerHTML = panels.map(function (p) {
      return '<div class="spec-panel" data-which="' + p.which + '"><img alt="">' +
        '<span class="spec-label">' + swatch(p.color) + esc(p.label) + "</span>" +
        '<div class="spec-faxis"></div><div class="spec-marks"></div>' +
        '<div class="spec-msg">' + (p.ok ? '<span class="spinner"></span>' : esc(p.wait)) + "</div></div>";
    }).join("");
    $all("#spec-stack .spec-panel").forEach(function (el) { el.__url = ""; });
  }

  function specParams(width, height) {
    var sp = S.spec;
    return "t0=" + sp.t0.toFixed(6) + "&t1=" + sp.t1.toFixed(6) + "&w=" + width + "&h=" + height + "&scale=" + sp.scale +
      "&floor=" + sp.floor[sp.mode] + (sp.mode === "diffs" && sp.matched ? "&matched=1" : "") + "&v=" + sp.data.split("|")[0] +
      "&map=" + MAP;
  }

  function loadSpec() {
    var t = S.test, sp = S.spec, stack = $("#spec-stack");
    if (!t || !stack) return;
    $all("[data-spec-mode]").forEach(function (b) { b.setAttribute("aria-pressed", String(b.dataset.specMode === sp.mode)); });
    $all("[data-spec-scale]").forEach(function (b) { b.setAttribute("aria-pressed", String(b.dataset.specScale === sp.scale)); });
    $("#spec-floor").value = String(sp.floor[sp.mode]);
    $("#spec-matched-wrap").hidden = sp.mode !== "diffs";
    $("#spec-matched").checked = sp.matched;
    $("#cb-low").textContent = sp.floor[sp.mode] + " dB";
    var span = sp.t1 - sp.t0;
    $("#spec-range").textContent = span >= t.source.duration - 1e-6 ? "Whole song, " + fmtClock(t.source.duration)
      : fmtAt(sp.t0, decimals(span)) + " – " + fmtAt(sp.t1, decimals(span)) + " (" + fmtSecs(span) + ")";
    $("#spec-note").innerHTML = sp.mode === "signals"
      ? "Each picture shows one recording over the same stretch of the song, on one colour scale. Drag across a picture to zoom in; tap to read the time and frequency."
      : "What is left when one recording is subtracted from the other, sample by sample. <b>Black means the samples are identical.</b>" +
        (sp.matched ? " With “Match levels first”, a plain volume difference is removed before subtracting." : "");
    $("#spec-note").innerHTML += " Red lines mark dropouts (gaps or jumps in a stream): the pictures skip over them to keep everything lined up with the song.";
    var dpr = Math.min(window.devicePixelRatio || 1, 2.5);
    var w = Math.max(200, Math.min(3000, Math.round(stack.clientWidth * dpr / 50) * 50));
    sp.width = stack.clientWidth;
    $all(".spec-panel", stack).forEach(function (el, i) {
      var p = sp.panels[i];
      if (!p || !p.ok) return;
      var h = Math.max(64, Math.min(1200, Math.round(el.clientHeight * dpr)));
      var url = "/api/tests/" + encodeURIComponent(t.id) + "/spectrogram/" + p.which + ".png?" + specParams(w, h);
      if (el.__url === url) return;
      el.__url = url;
      var msg = $(".spec-msg", el);
      msg.innerHTML = '<span class="spinner"></span>';
      msg.hidden = false;
      fetch(url).then(function (r) {
        if (!r.ok) return r.json().catch(function () { return {}; }).then(function (d) { throw new Error(d.error || "HTTP " + r.status); });
        var info = JSON.parse(r.headers.get("X-Spectrogram") || "{}");
        return r.blob().then(function (b) { return { blob: b, info: info }; });
      }).then(function (res) {
        if (el.__url !== url) return;
        var img = $("img", el);
        if (img.__obj) URL.revokeObjectURL(img.__obj);
        img.__obj = URL.createObjectURL(res.blob);
        img.src = img.__obj;
        el.__info = res.info;
        msg.hidden = true;
        drawFreqAxis(el, res.info);
        drawMarks(el, p);
        if (i === 0) drawTimeAxis(res.info);
      }).catch(function (e) {
        if (el.__url !== url) return;
        msg.hidden = false;
        msg.textContent = e.message;
      });
    });
    drawTimeAxis({ t0: sp.t0, t1: sp.t1 });
  }

  function decimals(span) { return span > 120 ? 0 : span > 10 ? 1 : span > 1 ? 2 : 3; }

  function drawTimeAxis(info) {
    var el = $("#spec-taxis"), t0 = info.t0, t1 = info.t1, span = t1 - t0;
    if (!el || !(span > 0)) return;
    var steps = [0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
    var want = Math.max(2, Math.floor(el.clientWidth / 80)), step = steps[steps.length - 1];
    for (var i = 0; i < steps.length; i++) if (span / steps[i] <= want) { step = steps[i]; break; }
    var dec = step >= 1 ? 0 : step >= 0.1 ? 1 : step >= 0.01 ? 2 : 3, html = "";
    for (var x = Math.ceil(t0 / step - 1e-9) * step; x <= t1 + 1e-9; x += step) {
      var pct = 100 * (x - t0) / span;
      if (pct < 3 || pct > 97) continue;
      html += '<span style="left:' + pct.toFixed(3) + '%">' + fmtAt(x, dec) + "</span>";
    }
    el.innerHTML = html;
  }

  function freqTicks(info) {
    var lo = info.fmin || 0, hi = info.fmax, ticks = [];
    if (S.spec.scale === "log") {
      [20, 50, 100, 200, 500, 1e3, 2e3, 5e3, 1e4, 2e4, 5e4, 1e5, 2e5].forEach(function (f) {
        if (f > lo && f < hi) ticks.push([f, 1 - Math.log(f / lo) / Math.log(hi / lo)]);
      });
    } else {
      var st = [1e3, 2e3, 5e3, 1e4, 2e4, 5e4].filter(function (s) { return hi / s <= 8; })[0] || 1e5;
      for (var f = st; f < hi; f += st) ticks.push([f, 1 - f / hi]);
    }
    return ticks.filter(function (x) { return x[1] > 0.06 && x[1] < 0.96; });
  }

  function drawFreqAxis(el, info) {
    $(".spec-faxis", el).innerHTML = freqTicks(info).map(function (x) {
      var f = x[0];
      return '<span style="top:' + (100 * x[1]).toFixed(2) + '%">' + (f >= 1000 ? f / 1000 + "k" : f) + "</span>";
    }).join("");
  }

  function drawMarks(el, p) {
    // Red ticks along the bottom: where the samples stop matching (dropouts, altered parts).
    var t = S.test, r = p.marks && t.results[p.marks], sp = S.spec, box = $(".spec-marks", el);
    var bands = r && r.plots && r.plots.timeline;
    if (!bands || r.verdict === "IDENTICAL") { box.innerHTML = ""; return; }
    var toSong = function (x) { return x; };
    if (p.marks === "ab") toSong = aToSong(t);  // these bands are on the first player's timeline
    var span = sp.t1 - sp.t0;
    box.innerHTML = bands.filter(function (b) { return b.kind !== "identical" && b.kind !== "ending"; }).map(function (b) {
      var a = toSong(b.start), e = Math.max(toSong(b.end), a);
      if (e < sp.t0 || a > sp.t1) return "";
      return '<span style="left:' + (100 * (a - sp.t0) / span).toFixed(3) + "%;width:" + (100 * (e - a) / span).toFixed(3) + '%"></span>';
    }).join("");
  }

  function aToSong(t) {
    // Seconds in the first player's recording -> seconds in the song, following its alignment
    // steps (a capture frame f lies in step k when f - lag_k falls inside that step).
    var al = (t.results.a || {}).alignment || {}, rate = (t.captures.a || {}).rate || 1, steps = al.steps;
    if (!steps || !steps.length) {
      var off = al.offset_seconds || 0;
      return function (x) { return x - off; };
    }
    return function (x) {
      var f = x * rate, k = 0;
      for (var i = 1; i < steps.length; i++) if (f - steps[i][1] >= steps[i][0]) k = i;
      return (f - steps[k][1]) / rate;
    };
  }

  function zoomTo(a, b) {
    var sp = S.spec, dur = S.test.source.duration, min = 0.02;
    a = Math.max(0, a); b = Math.min(dur, b);
    if (b - a < min) { var c = (a + b) / 2; a = Math.max(0, c - min / 2); b = Math.min(dur, a + min); }
    sp.t0 = a; sp.t1 = b;
    loadSpec();
  }

  function zoomCmd(cmd) {
    var sp = S.spec, dur = S.test.source.duration, span = sp.t1 - sp.t0, c = (sp.t0 + sp.t1) / 2;
    if (cmd === "all") zoomTo(0, dur);
    else if (cmd === "in") zoomTo(c - span / 4, c + span / 4);
    else if (cmd === "out") {
      var ns = Math.min(dur, span * 2), a = Math.max(0, Math.min(dur - ns, c - ns / 2));
      zoomTo(a, a + ns);
    } else if (cmd === "left") { var l = Math.max(0, sp.t0 - span / 2); zoomTo(l, l + span); }
    else if (cmd === "right") { var r = Math.min(dur, sp.t1 + span / 2); zoomTo(r - span, r); }
  }

  function bindSpecPointer(stack) {
    var drag = null, sel = null, cursor = null, readout = null, hideTimer = null;
    function pos(e) { var r = stack.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top, w: r.width }; }
    function timeAt(x, w) { var sp = S.spec; return sp.t0 + (sp.t1 - sp.t0) * Math.min(1, Math.max(0, x / w)); }
    function clearSel() { if (sel) { sel.remove(); sel = null; } }
    function hideReadout() { if (cursor) { cursor.remove(); cursor = null; } if (readout) { readout.remove(); readout = null; } }
    function showReadout(q) {
      hideReadout();
      var panel = $all(".spec-panel", stack).filter(function (el) { return q.y >= el.offsetTop && q.y < el.offsetTop + el.offsetHeight; })[0];
      if (!panel || !panel.__info) return;
      var info = panel.__info, rel = (q.y - panel.offsetTop) / panel.offsetHeight, f;
      f = info.scale === "log" ? info.fmin * Math.pow(info.fmax / info.fmin, 1 - rel) : info.fmax * (1 - rel);
      var t = timeAt(q.x, q.w), span = S.spec.t1 - S.spec.t0;
      cursor = document.createElement("div"); cursor.className = "spec-cursor"; cursor.style.left = q.x + "px";
      readout = document.createElement("div"); readout.className = "spec-readout";
      readout.textContent = fmtAt(t, Math.min(3, decimals(span) + 1)) + " · " + fmtHz(f);
      readout.style.top = Math.max(0, q.y - 30) + "px";
      readout.style[q.x > q.w * 0.7 ? "right" : "left"] = (q.x > q.w * 0.7 ? q.w - q.x + 8 : q.x + 8) + "px";
      stack.appendChild(cursor); stack.appendChild(readout);
    }
    stack.addEventListener("pointerdown", function (e) {
      if (e.button > 0) return;
      var q = pos(e);
      drag = { x0: q.x, x1: q.x, y: q.y, w: q.w, id: e.pointerId, moved: false };
      try { stack.setPointerCapture(e.pointerId); } catch (err) { /* old browsers */ }
    });
    stack.addEventListener("pointermove", function (e) {
      var q = pos(e);
      if (drag && e.pointerId === drag.id) {
        drag.x1 = q.x;
        if (Math.abs(drag.x1 - drag.x0) > 8) drag.moved = true;
        if (drag.moved) {
          hideReadout();
          if (!sel) { sel = document.createElement("div"); sel.className = "spec-sel"; stack.appendChild(sel); }
          var a = Math.max(0, Math.min(drag.x0, drag.x1)), b = Math.min(drag.w, Math.max(drag.x0, drag.x1));
          sel.style.left = a + "px"; sel.style.width = (b - a) + "px";
        }
      } else if (e.pointerType === "mouse") showReadout(q);
    });
    stack.addEventListener("pointerup", function (e) {
      if (!drag || e.pointerId !== drag.id) return;
      var d = drag; drag = null; clearSel();
      if (d.moved) zoomTo(timeAt(Math.min(d.x0, d.x1), d.w), timeAt(Math.max(d.x0, d.x1), d.w));
      else {
        showReadout({ x: d.x0, y: d.y, w: d.w });
        clearTimeout(hideTimer);
        if (e.pointerType !== "mouse") hideTimer = setTimeout(hideReadout, 4000);
      }
    });
    stack.addEventListener("pointercancel", function () { drag = null; clearSel(); });
    stack.addEventListener("pointerleave", function (e) { if (e.pointerType === "mouse" && !drag) hideReadout(); });
  }

  // ---------------------------------------------------------------- charts

  function renderCharts() {
    var t = S.test, el = $("#t-charts"), an = t.analysis;
    el.hidden = t.source.state !== "ready";
    if (el.hidden) return;
    var items = ["source", "a", "b"].filter(function (k) { return an[k] && an[k].plots && (k === "source" || (t.captures[k] || {}).state === "ready"); });
    var pairs = ["a", "b", "ab"].filter(function (k) { var r = t.results[k]; return r && r.state === "ready" && r.plots && (r.plots.spectrum || r.plots.envelope); });
    var zooms = ["a", "b", "ab"].filter(function (k) { var r = t.results[k]; return r && r.state === "ready" && r.zoom; });
    var html = "<h2>Charts</h2><div class=\"grid2\">" +
      chartBlock("ch-spec", "Average spectrum", "The level of each frequency over the whole song. Identical data draws identical lines, one on top of the other.") +
      chartBlock("ch-env", "Level over time", "Loudness through the song, all lined up with it.") + "</div>";
    pairs.forEach(function (k) {
      var lab = pairLabel(t, k);
      html += '<h3 style="margin-top:18px">Difference: ' + esc(lab) + "</h3>" +
        '<p class="muted small">After lining up the two recordings and matching their levels (' + esc(gainText(t.results[k].gain_db) || "no change") + "), this is what remains.</p>" +
        '<div class="grid2">' + chartBlock("ch-dspec-" + k, "Spectrum of what remains", "") + chartBlock("ch-denv-" + k, "What remains over time", "") + "</div>";
    });
    zooms.forEach(function (k) {
      var z = t.results[k].zoom;
      html += '<div class="chart-block" style="margin-top:18px"><h3>Close-up of the waveforms: ' + esc(pairLabel(t, k)) + "</h3>" +
        '<p class="muted">Channel ' + z.channel + ", sample by sample, around " + fmtAt(z.at_seconds, 3) + " of the " +
        (k === "ab" ? esc(t.players.a) + " recording" : "song") + (z.why === "largest" ? ", where they differ most." : ", where they first differ.") +
        '</p><canvas class="chart" id="ch-zoom-' + k + '"></canvas><div class="legend" id="lg-zoom-' + k + '"></div></div>';
    });
    var bits = items.filter(function (k) { return an[k].plots.bits; });
    if (bits.length) {
      html += '<div class="chart-block" style="margin-top:18px"><h3>Bit usage</h3><p class="muted">How often each bit of the 32-bit sample word is set, most significant bit on the left. ' +
        "Unused low bits (padding) stay empty; dither or processing fills them.</p>" +
        bitsHtml(bits.map(function (k) { return { name: whoName(t, k), bits: an[k].plots.bits, color: COLORS[k] }; })) + "</div>";
    }
    if (!setHtml(el, html)) return;
    var shift = function (k) {
      if (k === "source") return 0;
      var al = (t.results[k] || {}).alignment || {}, rate = (t.captures[k] || {}).rate || 1;
      return al.steps && al.steps.length ? al.steps[0][1] / rate : al.offset_seconds || 0;
    };
    var spec = items.map(function (k) {
      var p = an[k].plots.spectrum;
      return p && p.freqs ? { label: whoName(t, k), color: COLORS[k], x: p.freqs, y: p.db } : null;
    }).filter(Boolean);
    var env = items.map(function (k) {
      var p = an[k].plots.envelope, s = shift(k);
      return p && p.t ? { label: whoName(t, k), color: COLORS[k], x: p.t.map(function (x) { return x - s; }), y: p.db } : null;
    }).filter(Boolean);
    spectrumChart($("#ch-spec"), spec); legend($("#lg-ch-spec"), spec);
    timeChart($("#ch-env"), env, "dBFS", t.source.duration); legend($("#lg-ch-env"), env);
    pairs.forEach(function (k) {
      var pl = t.results[k].plots, music = { label: "music", color: MUSIC };
      if (pl.spectrum) {
        var s1 = [Object.assign({ x: pl.spectrum.freqs, y: pl.spectrum.signal_db }, music), { label: "what remains", color: DIFF, x: pl.spectrum.freqs, y: pl.spectrum.residual_db }];
        spectrumChart($("#ch-dspec-" + k), s1, 230); legend($("#lg-ch-dspec-" + k), s1);
      }
      if (pl.envelope) {
        var s2 = [Object.assign({ x: pl.envelope.t, y: pl.envelope.signal_db }, music), { label: "what remains", color: DIFF, x: pl.envelope.t, y: pl.envelope.residual_db }];
        timeChart($("#ch-denv-" + k), s2, "dBFS", null, 230); legend($("#lg-ch-denv-" + k), s2);
      }
    });
    zooms.forEach(function (k) {
      var z = t.results[k].zoom, ref = k === "ab" ? "a" : "source", cap = k === "ab" ? "b" : k;
      var x = z.ref.map(function (_, i) { return 1000 * (z.start_seconds + i / z.rate - z.at_seconds); });
      var series = [{ label: whoName(t, ref), color: COLORS[ref], x: x, y: z.ref }, { label: whoName(t, cap), color: COLORS[cap], x: x, y: z.cap }];
      chart($("#ch-zoom-" + k), { series: series, xMin: x[0], xMax: x[x.length - 1], linear: true, yLabel: "sample value",
        xFmt: function (v) { return (Math.round(v * 100) / 100) + " ms"; } });
      var big = 0;
      z.ref.forEach(function (v, i) { big = Math.max(big, Math.abs(z.cap[i] - v)); });
      legend($("#lg-zoom-" + k), series, "largest difference here: " + (big ? fmtDb(20 * Math.log10(big)) + " dBFS (" + big.toPrecision(3) + ")" : "none"));
    });
  }

  function pairLabel(t, k) {
    return k === "ab" ? t.players.b + " − " + t.players.a : t.players[k] + " − original";
  }

  function chartBlock(id, title, note) {
    return '<div class="chart-block"><h3>' + esc(title) + "</h3>" + (note ? '<p class="muted">' + esc(note) + "</p>" : "") +
      '<canvas class="chart" id="' + id + '"></canvas><div class="legend" id="lg-' + id + '"></div></div>';
  }

  function bitsHtml(rows) {
    return '<div class="bits">' + rows.map(function (r) {
      return '<div class="bitrow"><span>' + swatch(r.color) + esc(r.name) + '</span><div class="bitcells">' + r.bits.map(function (f, i) {
        var a = Math.min(1, f * 2);
        return '<span title="bit ' + (31 - i) + ": " + (100 * f).toFixed(1) + '%" style="' + (a > 0 ? "background:" + r.color + ";opacity:" + (0.25 + 0.75 * a).toFixed(2) : "") + '"></span>';
      }).join("") + "</div></div>";
    }).join("") + '<div class="bitaxis"><span>bit 31</span><span>bit 16</span><span>bit 0</span></div></div>';
  }

  function legend(el, series, extra) {
    if (!el) return;
    el.innerHTML = series.map(function (s) { return '<span><i style="background:' + s.color + '"></i>' + esc(s.label) + "</span>"; }).join("") +
      (extra ? '<span class="muted">' + esc(extra) + "</span>" : "");
  }

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
    var L = o.linear ? 64 : 44, R = 10, T = 8, B = 22, pw = W - L - R, ph = H - T - B;
    var ys = [];
    o.series.forEach(function (s) { s.y.forEach(function (v, i) { if (v != null && isFinite(v) && s.x[i] >= o.xMin && s.x[i] <= o.xMax) ys.push(v); }); });
    ctx.font = "11px system-ui, sans-serif";
    if (!ys.length) { ctx.fillStyle = cssVar("--muted"); ctx.fillText("nothing to show (digital silence)", L + 10, T + 20); return; }
    var ymax = Math.max.apply(null, ys), ymin = Math.min.apply(null, ys);
    if (o.linear) {
      var pad = (ymax - ymin) * 0.08 || Math.abs(ymax) * 0.1 || 1e-6;
      ymax += pad; ymin -= pad;
    } else {
      ymax = Math.ceil((ymax + 3) / 10) * 10;
      ymin = Math.max(Math.floor((ymin - 6) / 10) * 10, ymax - (o.span || 180));
      if (ymax - ymin < 20) ymin = ymax - 20;
    }
    var xmin = o.xMin, xmax = o.xMax;
    if (!(xmax > xmin)) xmax = xmin + 1;
    var lx = function (x) { return o.xLog ? Math.log10(x) : x; };
    var X = function (x) { return L + (lx(x) - lx(xmin)) / (lx(xmax) - lx(xmin)) * pw; };
    var Y = function (y) { return T + (1 - (y - ymin) / (ymax - ymin)) * ph; };
    ctx.strokeStyle = cssVar("--line"); ctx.fillStyle = cssVar("--muted"); ctx.lineWidth = 1;
    var ystep = niceStep(ymax - ymin, 5);
    ctx.textAlign = "right"; ctx.textBaseline = "middle";
    for (var y = Math.ceil(ymin / ystep) * ystep; y <= ymax; y += ystep) {
      ctx.beginPath(); ctx.moveTo(L, Y(y)); ctx.lineTo(W - R, Y(y)); ctx.stroke();
      ctx.fillText(o.linear ? Number(y.toPrecision(3)).toString() : String(Math.round(y)), L - 6, Y(y));
    }
    ctx.textAlign = "center"; ctx.textBaseline = "top";
    var xt = o.xLog ? [10, 20, 50, 100, 200, 500, 1e3, 2e3, 5e3, 1e4, 2e4, 5e4, 1e5, 2e5, 5e5] : (function () {
      var st = niceStep(xmax - xmin, Math.max(3, Math.floor(pw / 80))), out = [];
      for (var x = Math.ceil(xmin / st) * st; x <= xmax + st * 1e-6; x += st) out.push(x);
      return out;
    })();
    xt.forEach(function (x) {
      if (x < xmin || x > xmax) return;
      ctx.beginPath(); ctx.moveTo(X(x), T); ctx.lineTo(X(x), T + ph); ctx.stroke();
      ctx.fillText(o.xFmt(x), X(x), T + ph + 5);
    });
    ctx.save();
    ctx.beginPath(); ctx.rect(L, T, pw, ph); ctx.clip();
    o.series.forEach(function (s, k) {
      ctx.strokeStyle = s.color; ctx.lineWidth = o.linear ? 1.4 : 1.6;
      if (o.linear && k > 0) ctx.setLineDash([5, 3]);
      ctx.beginPath();
      var pen = false;
      for (var i = 0; i < s.x.length; i++) {
        var v = s.y[i];
        if (v == null || !isFinite(v) || s.x[i] < xmin || s.x[i] > xmax) { pen = false; continue; }
        var px = X(s.x[i]), py = Y(Math.max(v, ymin));
        if (pen) ctx.lineTo(px, py); else ctx.moveTo(px, py);
        pen = true;
      }
      ctx.stroke();
      ctx.setLineDash([]);
      if (o.linear && s.x.length <= 400) {
        ctx.fillStyle = s.color;
        for (var j = 0; j < s.x.length; j++) { if (s.y[j] != null) { ctx.beginPath(); ctx.arc(X(s.x[j]), Y(s.y[j]), 1.6, 0, 6.3); ctx.fill(); } }
      }
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

  function spectrumChart(canvas, series, span) {
    if (!series.length) return;
    var top = Math.max.apply(null, series.map(function (s) { return s.x[s.x.length - 1]; }));
    chart(canvas, { series: series, xLog: true, xMin: 20, xMax: top, yLabel: "dB", span: span || 160,
      xFmt: function (x) { return x >= 1000 ? (x / 1000) + "k" : String(x); } });
  }

  function timeChart(canvas, series, unit, end, span) {
    if (!series.length) return;
    var top = end || Math.max.apply(null, series.map(function (s) { return s.x[s.x.length - 1] || 1; }));
    chart(canvas, { series: series, xLog: false, xMin: 0, xMax: top, yLabel: unit, span: span || 160,
      xFmt: function (x) { return x >= 60 ? fmtClock(x) : Math.round(x * 10) / 10 + " s"; } });
  }

  // ---------------------------------------------------------------- details and downloads

  function renderDetails() {
    var t = S.test, el = $("#t-details"), an = t.analysis;
    var cols = ["source", "a", "b"].filter(function (k) { return an[k] && (k === "source" || (t.captures[k] || {}).state === "ready"); });
    el.hidden = cols.length < 1 || t.source.state !== "ready";
    if (el.hidden) return;
    function cap(k) { return (an[k] && an[k].capture) || {}; }
    var rows = [
      ["Format", function (k) { return k === "source" ? esc(an[k].label) : esc(cap(k).alsa_format || "–") + ' <span class="muted">(' + (an[k].bits) + "-bit words)</span>"; }],
      ["Sample rate", function (k) { return fmtRate(an[k].rate); }, true],
      ["Channels", function (k) { return String(an[k].channels); }, true],
      ["Bits in use", function (k) {
        var a = an[k];
        if (a.is_float) return a.resolution ? a.resolution + " (float)" : "float";
        return a.resolution == null ? "–" : [String(a.resolution), a.resolution + (a.resolution < a.bits ? ' <span class="muted">of ' + a.bits + "</span>" : "")];
      }, true],
      ["Length", function (k) { return fmtSecs(an[k].duration); }],
      ["Silence before / after", function (k) { var a = an[k]; return fmtSecs(a.lead_silence / a.rate) + " / " + fmtSecs(a.trail_silence / a.rate); }],
      ["Peak", function (k) { return (an[k].channel_stats || []).map(function (s) { return fmtDb(s.peak_db, 2); }).join(" / ") + " dBFS"; }, true],
      ["Loudness (RMS)", function (k) { return (an[k].channel_stats || []).map(function (s) { return fmtDb(s.rms_db, 2); }).join(" / ") + " dBFS"; }, true],
      ["Clipped samples", function (k) { var n = (an[k].channel_stats || []).reduce(function (s, x) { return s + x.clipped; }, 0); return n ? fmtNum(n) : "none"; }, true],
      ["Fingerprint", function (k) { var f = an[k].fingerprint; return f ? swatch(fpColor(f)) + ' <span class="fp">' + esc(f.slice(0, 12)) + "</span>" : "–"; }, true],
      ["Sent by", function (k) { return k === "source" ? "–" : esc((cap(k).player || {}).name || "–"); }],
      ["Player buffer / period", function (k) {
        var c = cap(k);
        return c.buffer_size && c.rate ? (1000 * c.buffer_size / c.rate).toFixed(1) + " / " + (1000 * c.period_size / c.rate).toFixed(1) + " ms" : "–";
      }],
      ["Recording", function (k) {
        if (k === "source") return "–";
        var c = cap(k), notes = [];
        if (c.capture_overruns) notes.push(c.capture_overruns + " overruns (SQ-tool lost data)");
        if (c.player_xruns_seen) notes.push(c.player_xruns_seen + " player underruns");
        if (c.usb) {
          if (c.usb.usbmon_dropped) notes.push(c.usb.usbmon_dropped + " USB events lost");
          if (c.usb.iso_errors) notes.push(c.usb.iso_errors + " USB packet errors");
        }
        if (c.interruptions && c.interruptions.length) notes.push(c.interruptions.length + " stream restarts");
        return notes.length ? esc(notes.join(", ")) : "complete";
      }],
      ["Stopped because", function (k) { return k === "source" ? "–" : esc(cap(k).stop_reason || "–"); }],
      ["CPU while recording", function (k) {
        var u = an[k].cpu;
        if (!u || u.system_percent == null) return "–";
        var top = (u.top || [])[0];
        return u.system_percent.toFixed(1) + "% of the machine" + (top ? " (busiest: " + esc(top.name) + " " + top.percent_of_one_cpu.toFixed(1) + "%)" : "");
      }],
      ["Recorded", function (k) { return k === "source" ? esc(t.source.filename) : esc(fmtTime(cap(k).started)); }]
    ];
    var html = '<h2>Details</h2><div class="table-wrap"><table class="cmp"><thead><tr><th></th>' + cols.map(function (k) {
      return '<th><div class="colhead">' + swatch(COLORS[k]) + esc(whoName(t, k)) + "</div></th>";
    }).join("") + "</tr></thead><tbody>" + rows.map(function (r) {
      // A cell is its HTML, or [what to compare, HTML].
      var cells = cols.map(function (k) { try { var v = r[1](k); return Array.isArray(v) ? v : [v, v]; } catch (e) { return ["–", "–"]; } });
      return "<tr><td>" + r[0] + "</td>" + cells.map(function (x, i) {
        return '<td class="' + (r[2] && i > 0 && x[0] !== cells[0][0] ? "diff" : "") + '">' + x[1] + "</td>";
      }).join("") + "</tr>";
    }).join("") + '</tbody></table></div><p class="muted small">Highlighted: differs from the original file. The same fingerprint means the same sample data.</p>';
    setHtml(el, html);
  }

  function renderDownloads() {
    var t = S.test, el = $("#t-downloads");
    el.hidden = t.source.state !== "ready";
    if (el.hidden) return;
    var base = "/api/tests/" + encodeURIComponent(t.id), links = [];
    var link = function (href, text) { links.push('<a class="btn small" href="' + href + '" download>' + text + "</a>"); };
    link(base + "/audio/source.wav", "⤓ The song (WAV)");
    ["a", "b"].forEach(function (k) { if ((t.captures[k] || {}).state === "ready") link(base + "/audio/" + k + ".wav", "⤓ What " + esc(t.players[k]) + " sent (WAV)"); });
    ["a", "b", "ab"].forEach(function (k) {
      var r = t.results[k];
      if (!r || r.state !== "ready" || r.verdict === "RESAMPLED" || r.verdict === "CHANNELS" || r.verdict === "NO MATCH") return;
      link(base + "/difference/" + k + ".wav", "⤓ Difference: " + esc(pairLabel(t, k)));
      if (r.alignment && r.alignment.model && r.verdict !== "IDENTICAL") link(base + "/difference/" + k + ".wav?matched=1", "⤓ Difference, levels matched: " + esc(pairLabel(t, k)));
    });
    setHtml(el, '<h2>Downloads</h2><div class="downloads">' + links.join("") + "</div>" +
      '<p class="muted small">Difference files are 32-bit float WAV on the song\'s timeline: digital silence where the samples are identical (dropouts are skipped to keep it lined up). ' +
      "Turn the volume down before playing one: a difference can be loud.</p>");
  }

  // ---------------------------------------------------------------- sheets

  function openSheet(html) {
    $("#sheet-body").innerHTML = html;
    $("#sheet").hidden = false;
    document.body.style.overflow = "hidden";
  }
  function closeSheet() {
    $("#sheet").hidden = true;
    $("#sheet-body").innerHTML = "";
    document.body.style.overflow = "";
  }

  function openSettings() {
    var st = S.state || {}, s = st.settings || { players: players(), device: "auto", idle_stop: 5 }, cap = st.capture || {};
    var opts = [["auto", "Automatic (the Loopback card)"]];
    if (cap.loopback) opts.push(["loopback:" + cap.loopback.card, "Loopback card " + cap.loopback.play_to]);
    (cap.dacs || []).forEach(function (d) { opts.push([d.id, "USB DAC: " + d.name + " (records the USB data, needs usbmon)"]); });
    if (!opts.some(function (o) { return o[0] === s.device; })) opts.push([s.device, s.device]);
    openSheet('<div class="stack"><h2 id="sheet-title">Settings</h2>' +
      '<div class="card stack"><h3>The two players</h3>' +
      '<label class="field">First player<input type="text" id="set-a" maxlength="40" value="' + esc(s.players.a) + '"></label>' +
      '<label class="field">Second player<input type="text" id="set-b" maxlength="40" value="' + esc(s.players.b) + '"></label>' +
      '<p class="muted small">Used for new tests. To rename the players of one test, use ⋯ on that test.</p></div>' +
      '<div class="card stack"><h3>Recording</h3><label class="field">Record from<select id="set-device">' + opts.map(function (o) {
        return '<option value="' + esc(o[0]) + '"' + (o[0] === s.device ? " selected" : "") + ">" + esc(o[1]) + "</option>";
      }).join("") + "</select></label>" +
      '<label class="field">Stop after this many seconds of digital silence<input type="number" id="set-idle" min="0" max="60" step="1" inputmode="numeric" value="' + esc(s.idle_stop) + '"></label>' +
      '<p class="muted small">A recording also stops by itself at the end of the song, or when the player closes the output.</p></div>' +
      '<button class="btn primary" id="set-save">Save</button>' +
      '<p class="muted small">SQ-tool ' + esc(st.version || "") + " · " + fmtBytes(st.free_bytes) + " free for recordings.</p></div>");
    $("#set-save").addEventListener("click", function () {
      api("/api/settings", { method: "POST", json: { players: { a: $("#set-a").value, b: $("#set-b").value }, device: $("#set-device").value, idle_stop: $("#set-idle").value } })
        .then(function () { closeSheet(); toast("Saved"); return poll(); }).catch(function (e) { toast(e.message); });
    });
  }

  function openHelp() {
    var cap = (S.state && S.state.capture) || {}, lp = cap.loopback;
    var dev = lp ? lp.play_to : "hw:Loopback,0";
    openSheet('<div class="stack"><h2 id="sheet-title">Setting up the players</h2>' +
      '<div class="card stack"><h3>How it works</h3><p>Linux has a virtual sound card called <b>Loopback</b>. A player plays to it like to any DAC, and SQ-tool records exactly the samples it receives: what the player would send to your DAC. Nothing is changed in the players.</p>' +
      (lp ? '<div class="alert ok">The Loopback card is ready: card ' + lp.card + ", play to <code>" + esc(dev) + "</code>.</div>"
        : '<div class="alert warn"><p>The Loopback card is not loaded yet.</p>' + (cap.can_load_loopback ? '<p><button class="btn small" data-act="load-loopback">Load it now</button></p>' : "<p>On the server run:</p><pre class=\"cmd\">sudo modprobe snd-aloop</pre>") + "</div>") +
      "</div>" +
      '<div class="card stack"><h3>Roon</h3><ol class="lines">' +
      "<li>In Roon, open <b>Settings → Audio</b>. Under your Roon Server, find <b>Loopback</b> and press <b>Enable</b> (if it is listed twice, either one works). Name the zone, for example “SQ-tool”.</li>" +
      "<li>In its <b>Device Setup</b>, use the same settings as your DAC's zone, so the test shows what your DAC gets. For a pure bit-perfect check: <b>Volume control: Fixed volume</b>, and in the zone's <b>DSP Engine</b> everything off (volume leveling, headroom, sample rate conversion, EQ).</li>" +
      "<li>Choose that zone, press Record in SQ-tool, then play the song from the start.</li>" +
      "<li>Loopback not listed? Load the driver above, then restart Roon Server.</li></ol></div>" +
      '<div class="card stack"><h3>Lyrion Music Server (Squeezelite)</h3>' +
      "<p>Lyrion plays through a player such as Squeezelite. Run a second Squeezelite on the server that plays to the Loopback card:</p>" +
      '<pre class="cmd">squeezelite -n SQ-tool -m 02:00:00:00:00:01 -o hw:CARD=Loopback,DEV=0 -s 127.0.0.1</pre>' +
      '<ol class="lines"><li>It appears in Lyrion as the player “SQ-tool”. Give it the same settings as your DAC\'s player. For a pure bit-perfect check, in its <b>Audio</b> settings: <b>Volume Control: output level fixed at 100%</b>, <b>Replay Gain: off</b>, <b>Crossfade: no fade</b>, and <b>Bitrate Limiting: no limit</b>.</li>' +
      "<li>Press Record in SQ-tool, then play the song to “SQ-tool” from the start. Squeezelite keeps its output open and silent while it is on: SQ-tool waits for the music and times the song from there.</li></ol></div>" +
      '<div class="card stack"><h3>Mandarin and other players</h3><ol class="lines">' +
      "<li>Choose the output device <b>Loopback</b> (<code>" + esc(dev) + "</code>) the same way you would choose your DAC.</li>" +
      "<li>Use the settings you normally use with your DAC, then press Record in SQ-tool and play the song from the start.</li>" +
      "<li>The two players of a test can have any names: change them in Settings, or with ⋯ on a test.</li></ol></div>" +
      '<div class="card stack"><h3>For a fair comparison</h3><ul class="lines">' +
      "<li>Play the same file in both players, from the beginning.</li>" +
      "<li>Record the same player twice: the two recordings should be identical, which shows the measurement is consistent.</li>" +
      "<li>If the levels differ even by a few tenths of a dB, that alone can make one player sound “better” in a sighted comparison.</li>" +
      "<li>Identical data means your DAC converts the same numbers from both. Any remaining audible difference would come from elsewhere (electrical noise, timing, system load) or from the listening test itself: level-matched blind listening is the way to check.</li></ul></div></div>");
  }

  function openTestMenu() {
    var t = S.test;
    if (!t) return;
    openSheet('<div class="stack"><h2 id="sheet-title">This test</h2><div class="card stack">' +
      '<label class="field">First player<input type="text" id="tm-a" maxlength="40" value="' + esc(t.players.a) + '"></label>' +
      '<label class="field">Second player<input type="text" id="tm-b" maxlength="40" value="' + esc(t.players.b) + '"></label>' +
      '<button class="btn primary" id="tm-save">Rename</button></div>' +
      '<div class="card stack"><p class="small muted">Song file: ' + esc(t.source.music_path || t.source.filename) + "<br>Created " + esc(fmtTime(t.created)) + "</p>" +
      '<button class="btn danger" id="tm-delete">Delete this test</button></div></div>');
    $("#tm-save").addEventListener("click", function () {
      api("/api/tests/" + encodeURIComponent(t.id), { method: "PATCH", json: { players: { a: $("#tm-a").value, b: $("#tm-b").value } } })
        .then(function () { closeSheet(); return loadTest(); }).catch(function (e) { toast(e.message); });
    });
    $("#tm-delete").addEventListener("click", function () {
      if (!confirm("Delete this test and its recordings?")) return;
      api("/api/tests/" + encodeURIComponent(t.id), { method: "DELETE" }).then(function () {
        closeSheet(); location.hash = "#/";
      }).catch(function (e) { toast(e.message); });
    });
  }

  // ---------------------------------------------------------------- actions

  document.addEventListener("click", function (e) {
    var el = e.target.closest ? e.target.closest("[data-act], [data-song], [data-dir], [data-close], [data-zoom], [data-spec-mode], [data-spec-scale]") : null;
    if (!el) return;
    var act = el.dataset.act;
    if (el.hasAttribute("data-close")) closeSheet();
    else if (el.dataset.song) createTest(el.dataset.song);
    else if (el.dataset.dir !== undefined) browse(el.dataset.dir);
    else if (el.dataset.zoom) zoomCmd(el.dataset.zoom);
    else if (el.dataset.specMode) { S.spec.mode = el.dataset.specMode; renderSpec(); }
    else if (el.dataset.specScale) { S.spec.scale = el.dataset.specScale; save("sq.scale", S.spec.scale); loadSpec(); }
    else if (act === "record") record(el.dataset.slot, el);
    else if (act === "stop") api("/api/record/stop", { method: "POST" }).then(poll).catch(function (err) { toast(err.message); });
    else if (act === "upload") $("#upload").click();
    else if (act === "help") openHelp();
    else if (act === "test-menu") openTestMenu();
    else if (act === "load-loopback") loadLoopback(el);
  });
  $("#btn-settings").addEventListener("click", openSettings);
  $("#btn-help").addEventListener("click", openHelp);
  $("#rec-badge").addEventListener("click", function () {
    var rec = S.state && S.state.recorder;
    if (rec && rec.test) location.hash = "#/test/" + encodeURIComponent(rec.test);
  });

  function record(slot, btn) {
    btn.disabled = true;
    api("/api/tests/" + encodeURIComponent(S.tid) + "/record", { method: "POST", json: { slot: slot } })
      .then(poll).catch(function (err) { toast(err.message); btn.disabled = false; });
  }

  function loadLoopback(btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Loading…';
    api("/api/loopback/load", { method: "POST" }).then(function (r) {
      toast(r.message + ". Restart Roon Server so it lists the Loopback output.");
      closeSheet();
      return poll();
    }).catch(function (err) {
      toast(err.message);
      btn.disabled = false;
      btn.textContent = "Try again";
    });
  }

  // ---------------------------------------------------------------- polling

  function pending(t) {
    if (!t) return false;
    if (t.source.state === "preparing") return true;
    var busy = function (o, state) { return Object.keys(o).some(function (k) { return o[k].state === state; }); };
    return busy(t.captures, "analysing") || busy(t.results, "comparing");
  }

  function pollDelay() {
    if (document.hidden) return 15000;
    var st = S.state;
    if ((st && (recActive(st.recorder) || st.busy)) || pending(S.test)) return 1000;
    return 3000;
  }

  function poll() {
    clearTimeout(S.timer);
    var tid = S.page === "test" ? S.tid : null;
    return api("/api/state" + (tid ? "?test=" + encodeURIComponent(tid) : "")).then(function (st) {
      S.state = st;
      $("#version").textContent = "SQ-tool " + st.version;
      $("#free").textContent = fmtBytes(st.free_bytes) + " free";
      $("#rec-badge").hidden = !hearing(st.recorder);
      renderSetup();
      if (S.page === "test" && S.tid === tid) {
        if (st.test_rev === null) return;
        if (st.test_rev !== S.rev) return loadTest();
        if (S.test) renderSteps();
      }
    }).catch(function () { /* server restarting: try again */ }).then(function () {
      clearTimeout(S.timer);
      S.timer = setTimeout(poll, pollDelay());
    });
  }

  document.addEventListener("visibilitychange", function () { if (!document.hidden) poll(); });

  var resizeTimer = null;
  window.addEventListener("resize", function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      S.charts.forEach(drawChart);
      var stack = $("#spec-stack");
      if (stack && S.test && Math.abs(stack.clientWidth - S.spec.width) > 40) loadSpec();
      else if (stack && S.test) loadSpec();
    }, 200);
  });

  route();
  poll();
})();
