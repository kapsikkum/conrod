/* Conrod desktop UI.
 *
 * Plain ES modules-free JavaScript on purpose: the app ships as one
 * PyInstaller executable, and a build step would mean carrying a Node
 * toolchain just to produce static assets.
 */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

const el = (tag, props = {}, ...kids) => {
  const node = Object.assign(document.createElement(tag), props);
  for (const kid of kids) if (kid != null) node.append(kid);
  return node;
};

async function api(path, options) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail = await res.text();
    try { detail = JSON.parse(detail).detail || detail; } catch {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

let toastTimer;
function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove("show"), 3000);
}

const state = {
  screen: "home",
  logAt: 0,
  logTimer: null,
  healthTimer: null,
  jobId: null,
  view: "review",
  number: null,
  search: "",
  offset: 0,
  limit: 120,
  total: 0,
  selected: new Set(),
  scanTimer: null,
  frameToken: -1,
  shownToken: null,
};

/* ── navigation ───────────────────────────────────────────── */

function show(screen) {
  state.screen = screen;
  $$(".screen").forEach((s) => { s.hidden = s.id !== `screen-${screen}`; });
  $$("#main-tabs button").forEach((b) =>
    b.classList.toggle("active", b.dataset.screen === screen));
  if (screen === "home") loadHome();
  if (screen === "setup") { loadSetup(); checkUpdate(); }
  if (screen === "settings") loadSettings();
  if (screen === "review") refreshReview();
}

$$("#main-tabs button").forEach((b) => { b.onclick = () => show(b.dataset.screen); });
document.addEventListener("click", (e) => {
  const target = e.target.closest("[data-goto]");
  if (target) show(target.dataset.goto);
});

/* ── home ─────────────────────────────────────────────────── */

async function loadHome() {
  const jobs = await api("/api/jobs");
  $("#job-count").textContent = jobs.length ? `${jobs.length}` : "";
  $("#home-empty").hidden = jobs.length > 0;

  $("#job-cards").replaceChildren(...jobs.map((job) => {
    const card = el("div", { className: "job-card" });
    const shot = el("div", { className: "shot" });
    // Use the job's first crop as the cover, the way a contact sheet would.
    shot.style.backgroundImage = `url(/api/jobs/${job.id}/cover)`;
    shot.append(el("span", {
      className: "badge",
      textContent: `${job.detection_count || 0} vehicles`,
    }));
    const left = job.unfinished_count || 0;
    card.append(
      shot,
      el("div", { className: "name", textContent: job.label || job.root }),
      el("div", { className: "sub",
                  textContent: left ? `${job.image_count} images · ${left} not done`
                                    : `${job.image_count} images` })
    );
    if (left) {
      // A big shoot that was stopped, paused or interrupted picks up where it
      // left off rather than starting again.
      const resume = el("button", { className: "resume", textContent: `Resume ${left}` });
      resume.onclick = async (e) => {
        e.stopPropagation();
        try {
          await api("/api/scan", { method: "POST", body: JSON.stringify({
            path: job.root, label: job.label, recursive: true, resume_job: job.id,
          })});
          show("scan");
          $("#scan-setup-pane").hidden = true;
          $("#scanner").hidden = false;
          $("#btn-stop").hidden = false;
          $("#btn-pause").hidden = false;
          pollScan();
        } catch (err) { toast(err.message); }
      };
      card.append(resume);
    }
    const menu = el("div", { className: "job-menu" });
    const rename = el("button", { className: "iconbtn", textContent: "Rename",
                                  title: "Rename this scan" });
    rename.onclick = async (e) => {
      e.stopPropagation();
      const name = prompt("Name for this scan", job.label || "");
      if (name === null) return;
      await api(`/api/jobs/${job.id}`, {
        method: "POST", body: JSON.stringify({ label: name }),
      });
      loadHome();
    };
    const remove = el("button", { className: "iconbtn danger", textContent: "Delete",
                                  title: "Forget this scan" });
    remove.onclick = async (e) => {
      e.stopPropagation();
      const what = job.label || job.root;
      if (!confirm(`Forget the scan "${what}"?

Its results and cached crops go. Your photos and any XMP already written are not touched.`)) return;
      await api(`/api/jobs/${job.id}`, { method: "DELETE" });
      if (state.jobId === job.id) state.jobId = null;
      toast("Scan deleted");
      loadHome();
    };
    menu.append(rename, remove);
    card.append(menu);

    card.onclick = () => { state.jobId = job.id; show("review"); };
    return card;
  }));

  const totals = jobs.reduce((acc, j) => ({
    images: acc.images + (j.image_count || 0),
    vehicles: acc.vehicles + (j.detection_count || 0),
    jobs: acc.jobs + 1,
  }), { images: 0, vehicles: 0, jobs: 0 });

  $("#rail-time").textContent = totals.images.toLocaleString();
  $("#rail-grid").replaceChildren(
    railStat(totals.vehicles.toLocaleString(), "Vehicles found"),
    railStat(String(totals.jobs), "Jobs")
  );
}

function railStat(value, label) {
  return el("div", {},
    el("div", { className: "n", textContent: value }),
    el("div", { className: "big-label", textContent: label }));
}

/* ── setup ────────────────────────────────────────────────── */

async function loadSetup() {
  const data = await api("/api/setup");
  const blocking = data.checks.filter((c) => !c.ok && c.required).length;
  const anyMissing = data.checks.some((c) => !c.ok);
  $("#setup-dot").hidden = !anyMissing;
  $("#rail-setup-note").textContent = data.ready
    ? (anyMissing ? "Ready. Some optional pieces are missing." : "Everything is installed.")
    : `${blocking} required item${blocking === 1 ? "" : "s"} missing.`;

  $("#check-list").replaceChildren(...data.checks.map((check) => {
    const row = el("li", {},
      el("span", { className: `icon ${check.ok ? "ok" : "no"}`,
                   textContent: check.ok ? "●" : "○" }),
      el("div", { className: "body" },
        el("div", { className: "name", textContent: check.label }),
        el("div", { className: "why", textContent: check.detail || "" }))
    );
    if (!check.ok && check.fix) {
      const button = el("button", { className: "primary", textContent: "Install" });
      button.onclick = async () => {
        button.disabled = true;
        await api("/api/setup/fix", {
          method: "POST", body: JSON.stringify({ name: check.fix }),
        });
        pollFix();
      };
      row.append(button);
    } else if (!check.ok && check.link) {
      row.append(el("a", { href: check.link, target: "_blank",
                           className: "muted", textContent: "Download ↗" }));
    }
    return row;
  }));

  if (data.fix && data.fix.active) pollFix();
}

let fixTimer;
async function pollFix() {
  clearInterval(fixTimer);
  $("#fix-progress").hidden = false;
  fixTimer = setInterval(async () => {
    const data = await api("/api/setup");
    const fix = data.fix || {};
    $("#fix-fill").style.width = `${fix.percent || 0}%`;
    $("#fix-status").textContent = fix.status || "";
    if (!fix.active) {
      clearInterval(fixTimer);
      setTimeout(() => { $("#fix-progress").hidden = true; loadSetup(); }, 900);
    }
  }, 700);
}

/* ── settings ─────────────────────────────────────────────── */

const SETTING_GROUPS = [
  ["Detection", [
    ["detect_model", "Detector model", "select", ["yolo11n.pt", "yolo11s.pt", "yolo11m.pt", "yolo11l.pt"],
      "Larger finds more, and is slower."],
    ["detect_imgsz", "Detection size", "number", null,
      "960 measured best on trackside panners; 1600 found fewer."],
    ["detect_conf", "Detection confidence", "number", null, "Lower finds more, with more false positives."],
    ["min_box_fraction", "Ignore vehicles smaller than", "number", null,
      "Fraction of the frame's short edge. Raise to skip background traffic."],
    ["max_vehicles_per_frame", "Max vehicles per frame", "number", null, ""],
    ["include_cars", "Cars", "bool", null, ""],
    ["include_bikes", "Motorcycles", "bool", null, ""],
    ["include_trucks", "Trucks and buses", "bool", null, ""],
  ]],
  ["Cropping", [
    ["crop_padding", "Crop padding", "number", null,
      "A tight box can clip the nose off a car and lose the plate."],
    ["dominant_subject_fraction", "Use whole frame above", "number", null,
      "When one vehicle fills this much of the frame, the box is the unreliable part."],
    ["crop_max_edge", "Max crop size (px)", "number", null, "Bigger keeps plates readable."],
  ]],
  ["Plates", [
    ["read_plates", "Read registration plates", "bool", null, ""],
    ["plate_conf", "Plate detector confidence", "number", null, ""],
    ["plate_ocr_edge", "Plate upscale (px)", "number", null, ""],
    ["write_plate_keyword", "Write the plate as a keyword", "bool", null, ""],
  ]],
  ["Numbers and text", [
    ["read_numbers", "Read competition numbers", "bool", null, ""],
    ["ocr_accept_confidence", "Accept OCR above", "number", null,
      "Below this, the vision model is consulted and the crop goes to review."],
    ["number_max_len", "Longest number", "number", null, ""],
    ["read_text", "Read livery and sponsor text", "bool", null, ""],
    ["text_min_confidence", "Text confidence floor", "number", null, ""],
  ]],
  ["Vision model", [
    ["use_vlm", "Identify make, model, colour, team", "bool", null,
      "Needs Ollama. Turning this off makes a scan several times faster."],
    ["vlm_model", "Model", "text", null, ""],
    ["vlm_host", "Ollama address", "text", null, ""],
    ["vlm_input_edge", "Input size (px)", "number", null, ""],
    ["identify_team", "Read team and sponsors", "bool", null, ""],
  ]],
  ["Output and speed", [
    ["write_sidecar_for_raw", "Write .xmp sidecars for RAW", "bool", null,
      "Off writes into the RAW file itself."],
    ["write_caption", "Also write a caption", "bool", null, ""],
    ["keyword_prefix", "Keyword prefix", "text", null, "e.g. TA: to mark machine-written keywords."],
    ["analysis_workers", "Analysis threads", "number", null,
      "3 measured 1.6x faster than 1. More than 4 wins nothing on 8 GB of VRAM."],
  ]],
];

let settingsCache = {};

async function loadSettings() {
  const data = await api("/api/settings");
  settingsCache = data.settings;
  const form = $("#settings-form");
  form.replaceChildren(...SETTING_GROUPS.map(([title, rows]) => {
    const group = el("div", { className: "setting-group" }, el("h3", { textContent: title }));
    for (const [key, label, kind, options, hint] of rows) {
      const value = settingsCache[key];
      let input;
      if (kind === "bool") {
        input = el("input", { type: "checkbox", checked: !!value });
        input.onchange = () => { settingsCache[key] = input.checked; };
      } else if (kind === "select") {
        input = el("select");
        for (const opt of options) {
          input.append(el("option", { value: opt, textContent: opt, selected: opt === value }));
        }
        input.onchange = () => { settingsCache[key] = input.value; };
      } else if (kind === "number") {
        input = el("input", { type: "number", value: String(value ?? ""), step: "any" });
        input.onchange = () => { settingsCache[key] = Number(input.value); };
      } else {
        input = el("input", { type: "text", value: value ?? "" });
        input.onchange = () => { settingsCache[key] = input.value; };
      }
      group.append(el("div", { className: "setting" },
        el("div", {},
          el("div", { className: "label", textContent: label }),
          hint ? el("div", { className: "hint", textContent: hint }) : null),
        input));
    }
    return group;
  }));

  // The entry list lives alongside the settings it affects.
  const mapGroup = el("div", { className: "setting-group" },
    el("h3", { textContent: "Entry list" }));
  const mapInput = el("input", { type: "text", value: data.map_path || "",
                                 placeholder: "path to entries.csv" });
  mapGroup.append(el("div", { className: "setting" },
    el("div", {},
      el("div", { className: "label", textContent: "Number → driver CSV" }),
      el("div", { className: "hint",
        textContent: data.map_size
          ? `${data.map_size} entries loaded`
          : "A 'number' column plus any others; each becomes a keyword." })),
    mapInput));
  mapInput.onchange = () => { settingsCache.__map = mapInput.value; };
  form.append(mapGroup);
}

$("#btn-save-settings").onclick = async () => {
  const mapPath = settingsCache.__map;
  const payload = { ...settingsCache };
  delete payload.__map;
  try {
    await api("/api/settings", {
      method: "POST",
      body: JSON.stringify({ settings: payload, map_path: mapPath ?? null }),
    });
    $("#settings-note").textContent = "Saved.";
    setTimeout(() => { $("#settings-note").textContent = ""; }, 2500);
    loadSettings();
  } catch (err) {
    toast(`Could not save: ${err.message}`);
  }
};

$("#btn-reset-settings").onclick = async () => {
  const data = await api("/api/settings");
  await api("/api/settings", {
    method: "POST", body: JSON.stringify({ settings: data.defaults }),
  });
  loadSettings();
  toast("Settings reset to defaults");
};

/* ── scanning ─────────────────────────────────────────────── */

$("#btn-pick").onclick = async () => {
  try {
    const { path } = await api("/api/pick-folder", { method: "POST" });
    if (path) { $("#scan-path").value = path; countFrames(); }
    return;
  } catch {
    // No dialog available; fall back to browsing in the page.
  }
  openBrowser($("#scan-path").value || "");
};

async function openBrowser(path) {
  const data = await api(`/api/browse?path=${encodeURIComponent(path)}`);
  $("#browser").hidden = false;
  $("#browse-here").textContent = data.path || "This PC";
  $("#browse-up").disabled = !data.parent;
  $("#browse-up").onclick = () => openBrowser(data.parent || "");
  $("#browse-use").onclick = () => {
    $("#scan-path").value = data.path;
    $("#browser").hidden = true;
    countFrames();
  };
  $("#browse-list").replaceChildren(...data.dirs.map((dir) => {
    const name = dir.replace(/\\$/, "").split("\\").pop() || dir;
    const li = el("li", { textContent: `📁  ${name}` });
    li.onclick = () => openBrowser(dir);
    return li;
  }));
}
$("#browse-close").onclick = () => { $("#browser").hidden = true; };

async function countFrames() {
  const path = $("#scan-path").value.trim();
  if (!path) return;
  $("#scan-count").textContent = "Counting…";
  try {
    const recursive = $("#scan-recursive").checked;
    const data = await api(
      `/api/browse/count?path=${encodeURIComponent(path)}&recursive=${recursive}`);
    $("#scan-count").textContent =
      `${data.frames.toLocaleString()} frames — about `
      + estimate(data.frames);
  } catch (err) {
    $("#scan-count").textContent = err.message;
  }
}
$("#btn-count").onclick = countFrames;

function estimate(frames) {
  // 5.5 s/frame measured with the vision model on, 3 workers; ~1.2 s without.
  const perFrame = settingsCache.use_vlm === false ? 1.2 : 5.5;
  const hours = (frames * perFrame) / 3600;
  if (hours < 1) return `${Math.max(1, Math.round(hours * 60))} min`;
  return `${hours.toFixed(1)} hours`;
}

$("#btn-scan").onclick = async () => {
  const path = $("#scan-path").value.trim();
  if (!path) { toast("Choose a folder first"); return; }
  try {
    await api("/api/scan", {
      method: "POST",
      body: JSON.stringify({
        path, label: $("#scan-label").value.trim() || null,
        recursive: $("#scan-recursive").checked,
      }),
    });
    $("#scan-setup-pane").hidden = true;
    $("#scanner").hidden = false;
    $("#btn-scan-review").hidden = true;
    $("#btn-stop").hidden = false;
    $("#btn-pause").hidden = false;
    pollScan();
  } catch (err) {
    toast(err.message);
  }
};

$("#scan-entries").onchange = async (e) => {
  const file = e.target.files?.[0];
  if (!file) return;
  $("#entries-status").textContent = "Reading…";
  try {
    const text = await file.text();
    const res = await api("/api/entries", {
      method: "POST", body: JSON.stringify({ name: file.name, text }),
    });
    $("#entries-status").textContent =
      `${file.name} — ${res.entries} entries` +
      (res.sample?.length ? ` (#${res.sample.join(", #")}…)` : "");
    toast(`Entry list loaded: ${res.entries} entries`);
    loadSettings();
  } catch (err) {
    $("#entries-status").textContent = err.message;
    toast(err.message);
  }
};

$("#btn-stop").onclick = async () => {
  await api("/api/scan/stop", { method: "POST", body: "{}" });
  toast("Stopping after the current frame — you can resume it later");
};

$("#btn-pause").onclick = async () => {
  const paused = $("#btn-pause").dataset.paused === "1";
  await api(`/api/scan/${paused ? "resume" : "pause"}`, { method: "POST", body: "{}" });
  toast(paused ? "Resuming" : "Paused — anything mid-analysis will still finish");
};

$("#btn-scan-review").onclick = () => show("review");

/* ── updates ──────────────────────────────────────────────── */
async function checkUpdate() {
  const note = $("#version-note"), box = document.querySelector(".version-box");
  const install = $("#btn-update-install");
  note.textContent = "Checking GitHub…";
  try {
    const u = await api("/api/update/check");
    $("#version-current").textContent = u.current;
    if (!u.ok) { note.textContent = u.error; return; }
    if (!u.newer) {
      note.textContent = `Up to date (latest is ${u.latest})`;
      box.classList.remove("update-available");
      install.hidden = true;
      return;
    }
    box.classList.add("update-available");
    const mb = u.size ? ` · ${(u.size / 1e6).toFixed(0)} MB` : "";
    if (u.from_source) {
      note.textContent = `${u.latest} is out${mb}, but this is running from `
                       + `source — update with git pull.`;
      install.hidden = true;
    } else {
      note.textContent = `Version ${u.latest} is available${mb}.`;
      install.hidden = false;
    }
  } catch (err) {
    note.textContent = err.message;
  }
}

$("#btn-update-check").onclick = checkUpdate;

$("#btn-update-install").onclick = async () => {
  if (!confirm(`Download the new version and restart Conrod?

The download is checked against the checksum published with the release before anything is replaced.`)) return;
  $("#btn-update-install").disabled = true;
  $("#update-progress").hidden = false;
  await api("/api/update/install", { method: "POST", body: "{}" });
  pollUpdate();
};

async function pollUpdate() {
  try {
    const u = await api("/api/update/status");
    const pct = u.total ? (u.done / u.total) * 100 : 0;
    $("#update-fill").style.width = `${pct}%`;
    $("#update-status").textContent = u.total
      ? `${u.message} — ${(u.done / 1e6).toFixed(0)} of ${(u.total / 1e6).toFixed(0)} MB`
      : u.message;
    if (u.state === "error") {
      $("#btn-update-install").disabled = false;
      toast(u.message);
      return;
    }
    if (u.state === "restarting") {
      $("#update-status").textContent = u.message;
      return;
    }
    if (u.state === "idle") { $("#btn-update-install").disabled = false; return; }
  } catch {}
  setTimeout(pollUpdate, 700);
}

/* ── health light ─────────────────────────────────────────── */
async function pollHealth() {
  try {
    const h = await api("/api/health");
    const node = $("#health");
    node.className = "health " + h.level + (h.scanning ? " busy" : "");
    $("#health-text").textContent = h.paused ? "Paused"
      : h.scanning ? "Scanning" : h.level === "ok" ? "Ready" : h.summary;
    node.title = h.problems?.length
      ? h.problems.map((p) => `${p.label}: ${p.detail || (p.required ? "missing" : "unavailable")}`).join(String.fromCharCode(10))
      : h.summary;
  } catch {
    $("#health").className = "health error";
    $("#health-text").textContent = "No connection";
  }
  clearTimeout(state.healthTimer);
  state.healthTimer = setTimeout(pollHealth, 8000);
}
$("#health").onclick = () => show("setup");

/* ── activity log ─────────────────────────────────────────── */
/* exiftool, the detector and the vision model all write to a console the
   packaged app does not have. This is where that output goes instead. */
$("#btn-activity").onclick = () => {
  const panel = $("#activity");
  panel.hidden = !panel.hidden;
  $("#btn-activity").textContent = panel.hidden ? "Show activity" : "Hide activity";
  if (!panel.hidden) pollLog();
};
$("#btn-activity-close").onclick = () => {
  $("#activity").hidden = true;
  $("#btn-activity").textContent = "Show activity";
};

async function pollLog() {
  if ($("#activity").hidden) return;
  try {
    const data = await api(`/api/log?after=${state.logAt || 0}`);
    if (data.lines?.length) {
      state.logAt = data.at;
      const body = $("#activity-body");
      const atBottom = body.scrollTop + body.clientHeight >= body.scrollHeight - 24;
      for (const line of data.lines) {
        const when = new Date(line.at * 1000).toLocaleTimeString();
        body.append(el("div", {
          className: `logline ${line.level}`,
          textContent: `${when}  ${line.text}` + (line.repeat > 1 ? `  (x${line.repeat})` : ""),
        }));
      }
      while (body.childElementCount > 400) body.firstElementChild.remove();
      if (atBottom) body.scrollTop = body.scrollHeight;
    }
  } catch {}
  clearTimeout(state.logTimer);
  state.logTimer = setTimeout(pollLog, 1000);
}

function pollScan() {
  clearInterval(state.scanTimer);
  state.scanTimer = setInterval(async () => {
    let data;
    try { data = await api("/api/scan"); } catch { return; }
    renderScan(data);
    if (!data.active) {
      clearInterval(state.scanTimer);
      $("#btn-stop").hidden = true;
      $("#btn-pause").hidden = true;
      $("#btn-scan-review").hidden = false;
      document.querySelector(".scanner").classList.remove("busy");
      if (data.error) toast(data.error);
      else toast(data.message || "Scan complete");
      if (data.job_id) state.jobId = data.job_id;
    }
  }, 500);
}

function renderScan(data) {
  const pause = $("#btn-pause");
  pause.dataset.paused = data.paused ? "1" : "0";
  pause.textContent = data.paused ? "Resume" : "Pause";
  pause.classList.toggle("accent", !!data.paused);

  const scanner = document.querySelector(".scanner");
  scanner.classList.toggle("busy", data.active && data.stage !== "analyse");

  const pct = data.total ? (data.done / data.total) * 100 : 0;
  $("#scan-fill").style.width = `${pct}%`;
  $("#scan-counter").textContent = `${data.done} / ${data.total || "?"}`;
  // Null means the estimate is not trustworthy yet. Saying so beats both a
  // blank line and a confident number derived from four frames.
  $("#scan-eta").textContent = data.eta != null
    ? `about ${formatSeconds(data.eta)} left`
    : (data.active ? "estimating…" : "");
  $("#scan-found").textContent = data.message || "";

  const current = data.current;
  if (!current) return;
  $("#scan-file").textContent = current.name || "";
  $("#scan-phase").textContent = data.active ? (current.phase || "SCANNING") : "DONE";

  // Only refetch the image when the frame actually changed.
  const img = $("#scan-img");
  if (data.frame_token !== state.frameToken) {
    state.frameToken = data.frame_token;
    state.shownToken = null;
    img.onload = () => {
      state.shownToken = data.frame_token;
      fitOverlay();
    };
    img.src = `/api/scan/frame?t=${data.frame_token}`;
  }

  const overlay = $("#scan-overlay");

  // Boxes belong to one specific frame. The image is fetched over HTTP and
  // arrives a moment later, so drawing them straight away paints this
  // frame's boxes on top of the previous frame's photo -- which is what made
  // the live view look like it was glitching. Wait for the image to land.
  if (state.shownToken !== data.frame_token) {
    overlay.replaceChildren();
    return;
  }
  fitOverlay();

  overlay.replaceChildren(...(current.boxes || []).map((box) => {
    const node = el("div", { className: "box" + (box.number ? "" : " pending") });
    node.style.left = `${box.x * 100}%`;
    node.style.top = `${box.y * 100}%`;
    node.style.width = `${box.w * 100}%`;
    node.style.height = `${box.h * 100}%`;
    const label = box.number
      ? `#${box.number} · ${((box.read_conf || 0) * 100).toFixed(1)}%`
      : `${box.kind} · ${((box.conf || 0) * 100).toFixed(1)}%`;
    node.append(el("div", { className: "box-label", textContent: label }));
    return node;
  }));

  const log = $("#scan-log");
  log.replaceChildren(...(current.log || []).map((line) =>
    el("div", { textContent: line })));
  if (data.active) log.append(el("span", { className: "caret" }));
}

// The photo is letterboxed inside the stage by object-fit: contain, but the
// overlay was pinned to the stage. Box coordinates are fractions of the
// *photo*, so on any frame whose aspect did not match the stage the boxes
// drifted sideways and ran off the edge. Size the overlay to the pixels the
// image actually occupies.
function fitOverlay() {
  const img = $("#scan-img");
  const overlay = $("#scan-overlay");
  const nw = img.naturalWidth, nh = img.naturalHeight;
  if (!nw || !nh) return;

  const boxW = img.clientWidth, boxH = img.clientHeight;
  const scale = Math.min(boxW / nw, boxH / nh);
  const drawnW = nw * scale, drawnH = nh * scale;

  overlay.style.left = `${img.offsetLeft + (boxW - drawnW) / 2}px`;
  overlay.style.top = `${img.offsetTop + (boxH - drawnH) / 2}px`;
  overlay.style.width = `${drawnW}px`;
  overlay.style.height = `${drawnH}px`;
  overlay.style.right = "auto";
  overlay.style.bottom = "auto";
}

window.addEventListener("resize", fitOverlay);

function formatSeconds(seconds) {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  // Past a day, tenths of an hour are false precision on an estimate this
  // rough -- "86.2 h left" read as a measurement rather than a guess.
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)} h`;
  const days = seconds / 86400;
  return days < 10 ? `${days.toFixed(1)} days` : `${Math.round(days)} days`;
}

/* ── review ───────────────────────────────────────────────── */

async function refreshReview() {
  const jobs = await api("/api/jobs");
  if (!jobs.length) {
    $("#empty").hidden = false;
    $("#empty").textContent = "No scans yet. Run one from the Scan tab.";
    $("#grid").replaceChildren();
    return;
  }
  const select = $("#job-select");
  select.replaceChildren(...jobs.map((j) =>
    el("option", { value: j.id, textContent: `${j.label || j.root} — ${j.image_count}` })));
  if (!state.jobId || !jobs.some((j) => j.id === state.jobId)) state.jobId = jobs[0].id;
  select.value = String(state.jobId);
  state.offset = 0;
  state.selected.clear();
  updateBulkBar();
  await Promise.all([loadSummary(), loadGrid(false)]);
}

async function loadSummary() {
  const data = await api(`/api/jobs/${state.jobId}/summary`);
  const c = data.counts;
  $("#stats").replaceChildren(
    stat("frames", data.images.total), stat("vehicles", c.detections),
    stat("numbered", c.numbered), stat("plates", c.plated),
    stat("to review", c.to_review), stat("written", data.images.written));

  $("#number-list").replaceChildren(...data.numbers.map((n) => {
    const li = el("li", { className: state.number === n.number ? "active" : "" },
      el("span", { className: "n", textContent: `#${n.number}` }),
      el("span", { className: "who", textContent: n.who || "" }),
      el("span", { className: "c", textContent: n.frames }));
    li.onclick = () => { state.number = n.number; setView("number"); };
    return li;
  }));
}

function stat(label, value) {
  return el("span", {}, el("b", { textContent: String(value ?? 0) }), ` ${label}`);
}

async function loadGrid(append = false) {
  // "By number" with nothing chosen yet is a valid state: show the sidebar
  // and wait for a pick rather than asking the server for an impossible view.
  if (state.view === "number" && !state.number) {
    $("#grid").replaceChildren();
    $("#more").hidden = true;
    $("#empty").hidden = false;
    $("#empty").textContent = "Pick a number from the list.";
    return;
  }

  const params = new URLSearchParams({
    view: state.view, limit: state.limit, offset: state.offset,
  });
  if (state.view === "number" && state.number) params.set("number", state.number);
  if (state.search) params.set("search", state.search);

  const data = await api(`/api/jobs/${state.jobId}/detections?${params}`);
  state.total = data.total;
  const blocks = groupItems(data.items).map(vehicleBlock);
  if (append) $("#grid").append(...blocks);
  else $("#grid").replaceChildren(...blocks);

  $("#more").hidden = state.offset + data.items.length >= state.total;
  $("#empty").hidden = state.total > 0;
  if (!state.total) {
    $("#empty").textContent = state.view === "review"
      ? "Nothing left to review. Write the XMP when you are ready."
      : "Nothing here.";
  }
}

// One vehicle, however many frames it appeared in. Detections with no group
// stand alone, keyed by their own id so they cannot collide with a group key.
function groupItems(items) {
  const order = [];
  const byKey = new Map();
  for (const item of items) {
    const key = item.group_size > 1 && item.group_key != null
      ? `g${item.group_key}` : `d${item.id}`;
    if (!byKey.has(key)) { byKey.set(key, []); order.push(key); }
    byKey.get(key).push(item);
  }
  return order.map((key) => byKey.get(key));
}

// The facts the group agreed on, shown once above its frames rather than
// repeated on every card. Each frame only sees the panels facing the camera,
// so the header is the accumulated answer and the cards below are evidence.
function vehicleBlock(members) {
  const lead = members[0];
  const section = el("section", { className: "vehicle" });

  const head = el("div", { className: "vehicle-head" });
  if (lead.colour_hex) {
    const swatch = el("span", { className: "swatch big" });
    swatch.style.background = lead.colour_hex;
    swatch.title = lead.colour_word
      ? `Sampled ${lead.colour_hex} — the model called it "${lead.colour_word}"`
      : `Sampled ${lead.colour_hex}`;
    head.append(swatch);
  }
  head.append(el("h3", { textContent: lead.title || lead.cls }));

  const facts = el("div", { className: "facts" });
  const number = members.find((m) => m.number)?.number;
  const plate = members.find((m) => m.plate)?.plate;
  const attrs = members.find((m) => (m.attributes || {}).team)?.attributes || {};
  if (number) facts.append(el("span", { className: "fact number", textContent: `#${number}` }));
  if (plate) facts.append(el("span", { className: "fact plate", textContent: plate }));
  if (attrs.team) facts.append(el("span", { className: "fact team", textContent: attrs.team }));

  const sponsors = attrs.sponsors || [];
  for (const name of sponsors.slice(0, 3)) {
    if (name && name !== attrs.team) {
      facts.append(el("span", { className: "fact soft", textContent: name }));
    }
  }
  if (members.length > 1) {
    const pct = Math.round((lead.group_agreement || 0) * 100);
    const disputed = lead.disputed?.length ? lead.disputed : null;
    facts.append(el("span", {
      className: "fact count" + (disputed ? " disputed" : ""),
      textContent: `${members.length} frames`,
      title: disputed
        ? `The readers disagreed: ${disputed.join(", ")}`
        : `${pct}% agreed across ${members.length} frames`,
    }));
  }
  head.append(facts);
  section.append(head, el("div", { className: "strip" }, ...members.map(card)));
  return section;
}

function band(value) {
  if (value == null) return "low";
  if (value >= 0.85) return "high";
  if (value >= 0.6) return "mid";
  return "low";
}

function card(item) {
  const attrs = item.attributes || {};
  const node = el("div", { className: "card" + (item.rejected ? " rejected" : "") });
  node.dataset.id = item.id;

  // Ask for a thumbnail, not the 2048px crop the readers work from.
  const thumb = el("img", { className: "thumb", src: `${item.crop_url}?w=420`,
                            loading: "lazy", decoding: "async",
                            alt: item.title || item.cls, title: "Click for the whole frame" });
  thumb.onclick = (e) => {
    if (e.shiftKey) { toggleSelect(node, item.id); return; }
    $("#frame-img").src = item.frame_url;
    $("#frame-caption").textContent = item.image_path;
    $("#frame-dialog").showModal();
  };

  const title = el("div", { className: "title", textContent: item.title || item.cls });
  if (item.colour_hex) {
    // The model's word for a colour is often wrong or useless -- two cars
    // that look nothing alike both come back "gray". This square is measured
    // off the crop, so a wrong word is obvious without opening the frame.
    const swatch = el("span", { className: "swatch" });
    swatch.style.background = item.colour_hex;
    swatch.title = item.colour_word
      ? `Sampled ${item.colour_hex} — the model called it "${item.colour_word}"`
      : `Sampled ${item.colour_hex}`;
    title.prepend(swatch, " ");
  }
  if (item.group_size > 1) {
    // Say how many crops the group had and how much of it agreed, so a name
    // eight frames settled on reads differently from a four-way tie.
    //
    // Three outcomes, not two: the group agreed on a full name, it agreed on
    // the make but not the model, or it agreed on nothing. The middle one
    // still shows a make, so calling it "no agreement" read as a
    // contradiction against the "blue Ford" printed next to it.
    const pct = Math.round((item.group_agreement || 0) * 100);
    const disputed = item.disputed?.length ? item.disputed : null;
    const makeOnly = disputed && item.make && !item.model;
    const badge = el("span", {
      className: "grouptag" + (disputed ? " disputed" : ""),
      textContent: makeOnly
        ? `${item.group_size} seen · ${pct}% say ${item.make}`
        : disputed
          ? `${item.group_size} seen · no agreement`
          : `${item.group_size} seen · ${pct}% agree`,
      title: disputed
        ? `The readers disagreed: ${disputed.join(", ")}`
        : `Agreed across ${item.group_size} frames of this vehicle`,
    });
    title.append(" ", badge);
  }
  const who = el("div", { className: "who", textContent: item.who || "" });
  const kw = el("div", { className: "kw",
                         textContent: (item.keywords || []).join(" · ") });

  const num = el("input", { className: "num", value: item.number || "",
                            placeholder: "—", inputMode: "numeric", autocomplete: "off" });
  const plate = el("input", { className: "plate", value: item.plate || "",
                              placeholder: "plate", autocomplete: "off" });

  // Show the confidence of whatever was actually read. This used to report
  // the race-number confidence unconditionally, so a card with a plate read at
  // 0.89 and no competition number displayed a red 0%.
  const readConf = item.number ? item.number_conf
                 : item.plate ? item.plate_conf : null;
  const readWhat = item.number ? "number" : item.plate ? "plate" : "";
  const conf = el("span", { className: "tag conf",
    title: readWhat ? `Confidence in the ${readWhat} read` : "Nothing was read",
    textContent: readConf != null && (item.number || item.plate)
      ? `${readWhat} ${Math.round(readConf * 100)}%` : "no read" });
  conf.dataset.band = (item.number || item.plate) ? band(readConf) : "none";

  const src = el("span", {
    className: "tag " + (item.number_source || "").replace("+", " "),
    textContent: item.number_source || "", hidden: !item.number_source });

  const teamTag = el("span", { className: "tag warn", textContent: "team unverified",
    hidden: !(attrs.team && !attrs.team_corroborated) });

  const save = async (patch) => {
    const saved = await api(`/api/detections/${item.id}`, {
      method: "POST", body: JSON.stringify({ ...patch, reviewed: true }),
    });
    node.classList.add("saved");
    title.textContent = saved.title || item.cls;
    who.textContent = saved.who || "";
    kw.textContent = (saved.keywords || []).join(" · ");
    conf.textContent = "confirmed"; conf.dataset.band = "high";
    src.textContent = "manual"; src.className = "tag manual"; src.hidden = false;
    teamTag.hidden = true;
    loadSummary();
  };

  num.onkeydown = async (e) => {
    if (e.key === "Enter") { e.preventDefault(); await save({ number: num.value.trim() }); focusNext(node); }
    else if (e.key === "Escape") { e.preventDefault(); await reject(item.id, true); node.classList.add("rejected"); focusNext(node); }
  };
  num.onblur = () => { if (num.value.trim() !== (item.number || "")) save({ number: num.value.trim() }); };
  plate.onblur = () => { if (plate.value.trim() !== (item.plate || "")) save({ plate: plate.value.trim() }); };

  const rejectBtn = el("button", { className: "ghost danger", textContent: "✕",
                                   title: "Not a competitor / unreadable" });
  rejectBtn.onclick = async () => {
    const now = !node.classList.contains("rejected");
    await reject(item.id, now);
    node.classList.toggle("rejected", now);
    loadSummary();
  };

  node.append(thumb, el("div", { className: "body" },
    title,
    el("div", { className: "file", textContent: item.filename, title: item.image_path }),
    el("div", { className: "row" }, num, plate, conf, src,
       el("div", { className: "actions" }, rejectBtn)),
    teamTag, who, kw));
  return node;
}

async function reject(id, rejected) {
  await api(`/api/detections/${id}`, {
    method: "POST", body: JSON.stringify({ rejected, reviewed: true }) });
}

function focusNext(fromCard) {
  const cards = $$(".card");
  for (let i = cards.indexOf(fromCard) + 1; i < cards.length; i += 1) {
    if (cards[i].classList.contains("rejected")) continue;
    const input = cards[i].querySelector("input.num");
    if (input) {
      input.focus(); input.select();
      cards[i].scrollIntoView({ block: "center", behavior: "smooth" });
      return;
    }
  }
}

function toggleSelect(node, id) {
  if (state.selected.has(id)) { state.selected.delete(id); node.classList.remove("selected"); }
  else { state.selected.add(id); node.classList.add("selected"); }
  updateBulkBar();
}

function updateBulkBar() {
  $("#bulk-bar").hidden = state.selected.size === 0;
  $("#bulk-count").textContent = `${state.selected.size} selected`;
}

function setView(view) {
  state.view = view;
  state.offset = 0;
  $$("#view-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  $("#sidebar").hidden = view !== "number";
  loadGrid(false);
  loadSummary();
}

$$("#view-tabs button").forEach((b) => { b.onclick = () => setView(b.dataset.view); });
$("#job-select").onchange = (e) => { state.jobId = Number(e.target.value); refreshReview(); };
$("#more").onclick = () => { state.offset += state.limit; loadGrid(true); };

let searchTimer;
$("#search").oninput = (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.search = e.target.value.trim();
    state.offset = 0;
    loadGrid(false);
  }, 250);
};

$("#frame-close").onclick = () => $("#frame-dialog").close();
$("#frame-dialog").onclick = (e) => { if (e.target.id === "frame-dialog") $("#frame-dialog").close(); };

$("#bulk-apply").onclick = async () => {
  await api("/api/detections/bulk", { method: "POST",
    body: JSON.stringify({ ids: [...state.selected], number: $("#bulk-number").value.trim() }) });
  toast(`Set ${state.selected.size} crops`);
  refreshReview();
};
$("#bulk-reject").onclick = async () => {
  await api("/api/detections/bulk", { method: "POST",
    body: JSON.stringify({ ids: [...state.selected], rejected: true }) });
  toast(`Rejected ${state.selected.size} crops`);
  refreshReview();
};
$("#bulk-clear").onclick = () => {
  state.selected.clear();
  $$(".card.selected").forEach((c) => c.classList.remove("selected"));
  updateBulkBar();
};

$("#btn-dry").onclick = async () => {
  const r = await api(`/api/jobs/${state.jobId}/write`, {
    method: "POST", body: JSON.stringify({ dry_run: true }) });
  toast(`${r.frames} frames would be keyworded`);
};

$("#btn-write").onclick = async () => {
  if (!confirm("Write keywords into XMP sidecars (RAW) and into the files themselves (JPEG)?")) return;
  const button = $("#btn-write");
  button.disabled = true; button.textContent = "Writing…";
  try {
    const r = await api(`/api/jobs/${state.jobId}/write`, {
      method: "POST", body: JSON.stringify({ dry_run: false }) });
    toast(`Wrote ${r.written} frames (${r.failed} failed, ${r.skipped} had nothing)`);
    loadSummary();
  } catch (err) {
    toast(`Write failed: ${err.message}`);
  } finally {
    button.disabled = false; button.textContent = "Write XMP";
  }
};

/* ── boot ─────────────────────────────────────────────────── */

(async function boot() {
  try {
    await loadSettings();
    // Render the setup state now, not on first visit — the home rail shows a
    // summary of it and would otherwise sit on "Checking…" forever.
    await loadSetup();
    const setup = await api("/api/setup");
    const scan = await api("/api/scan");

    $("#splash").classList.add("gone");
    setTimeout(() => { $("#splash").hidden = true; }, 500);
    $("#app").hidden = false;
    pollHealth();

    if (scan.active) {
      show("scan");
      $("#scan-setup-pane").hidden = true;
      $("#scanner").hidden = false;
      $("#btn-stop").hidden = false;
      $("#btn-pause").hidden = false;
      pollScan();
    } else if (!setup.ready) {
      show("setup");
    } else {
      show("home");
    }
  } catch (err) {
    $("#splash-msg").textContent = `Could not start: ${err.message}`;
  }
})();
