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
function toast(message, ms = 3000) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove("show"), ms);
}

const state = {
  screen: "home",
  album: null,
  sheetOffset: 0,
  lastStage: null,
  sort: "review",
  minStars: null,
  items: [],
  stack: null,            // which vehicle's gallery is open, if any
  cursor: null,
  dialogNode: null,
  logAt: 0,
  logTimer: null,
  healthTimer: null,
  jobId: null,
  view: "review",
  facet: "number",        // which list the sidebar is browsing
  facetPick: null,        // {kind, value} narrowing the grid, if any
  search: "",
  offset: 0,
  limit: 500,
  total: 0,
  selected: new Set(),
  scanning: false,          // a close while this is true asks first
  watchWanted: null,        // folder to watch once the album exists
  watchTimer: null,
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
  if (screen === "album") loadAlbum();
  if (screen === "review") refreshReview();
  // Coming to the Scan screen always means "I want to add a folder". The
  // setup pane used to be hidden the moment a scan started and never put
  // back, so during a run that takes hours -- exactly when the next card
  // gets added -- this screen showed only the run already going, and there
  // was no way to reach the form at all.
}

$$("#main-tabs button").forEach((b) => { b.onclick = () => show(b.dataset.screen); });
document.addEventListener("click", (e) => {
  const target = e.target.closest("[data-goto]");
  if (target) show(target.dataset.goto);
});

/* ── home ─────────────────────────────────────────────────── */

async function loadHome() {
  // The picker lives here, so whether a scan is already running decides
  // what it may offer before anyone presses anything.
  api("/api/scan").then((s) => reflectRunning(s.active)).catch(() => {});
  const jobs = await api("/api/jobs");
  $("#job-count").textContent = jobs.length ? `${jobs.length}` : "";
  $("#home-empty").hidden = jobs.length > 0;

  $("#job-cards").replaceChildren(...jobs.map((job) => {
    const card = el("div", { className: "job-card" });
    const shot = el("div", { className: "shot" });
    // Use the album's first crop as the cover, the way a contact sheet would.
    shot.style.backgroundImage = `url(/api/jobs/${job.id}/cover)`;
    const left = job.unfinished_count || 0;
    const found = job.detection_count || 0;

    // There is no cover until something has been found, and an album part way
    // through a long shoot has nothing to show for a while. An empty black
    // rectangle reads as a broken card rather than as work in hand, so say
    // which it is.
    if (!found) {
      // Say which of several different nothings this is. "No vehicles
      // found" on an album that has not been looked at yet is a lie.
      const said = job.status === "indexed" ? "Indexed — ready to cull"
                 : job.status === "culled"  ? "Culled — ready to identify"
                 : left ? "In progress…" : "No vehicles found";
      shot.append(el("div", { className: "state", textContent: said }));
    } else {
      shot.append(el("span", {
        className: "badge", textContent: `${found} vehicles`,
      }));
    }
    card.append(
      shot,
      el("div", { className: "name", textContent: job.label || job.root }),
      el("div", { className: "sub",
                  textContent: left ? `${job.image_count} images · ${left} not done`
                                    : `${job.image_count} images` })
    );
    // What this album is waiting for. An indexed album has frames and no
    // vehicles; a culled one has vehicles and no names. Offering the next
    // step by name beats a single "Resume" that means something different
    // depending on how far the album got.
    const steps = el("div", { className: "steps" });
    const step = (label, stage, hint) => {
      const button = el("button", { className: "step", textContent: label,
                                    title: hint });
      button.onclick = (e) => { e.stopPropagation(); runStage(job, stage); };
      steps.append(button);
    };
    if (job.status === "indexed") {
      step("Cull", "cull",
           "Detect the vehicles and rate them. No vision model, about a second a frame.");
    } else if (job.status === "culled") {
      step("Identify", "identify",
           "Name what survived the cull. This is the slow one.");
    }
    if (found) {
      const write = el("button", { className: "step", textContent: "Write XMP",
                                   title: "Write keywords to the sidecars" });
      write.onclick = (e) => {
        e.stopPropagation();
        state.jobId = job.id;
        show("review");
        toast("Review the album, then Write XMP");
      };
      steps.append(write);
    }
    if (steps.children.length) card.append(steps);

    if (left && job.status !== "indexed") {
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
                                  title: "Rename this album" });
    rename.onclick = async (e) => {
      e.stopPropagation();
      const name = prompt("Name for this album", job.label || "");
      if (name === null) return;
      await api(`/api/jobs/${job.id}`, {
        method: "POST", body: JSON.stringify({ label: name }),
      });
      loadHome();
    };
    const remove = el("button", { className: "iconbtn danger", textContent: "Delete",
                                  title: "Forget this album" });
    remove.onclick = async (e) => {
      e.stopPropagation();
      const what = job.label || job.root;
      if (!confirm(`Forget the album "${what}"?

Its results and cached crops go. Your photos and any XMP already written are not touched.`)) return;
      await api(`/api/jobs/${job.id}`, { method: "DELETE" });
      if (state.jobId === job.id) state.jobId = null;
      toast("Album deleted");
      loadHome();
    };
    menu.append(rename, remove);
    card.append(menu);

    // The album, not the vehicle grid: an album that has only been
    // indexed has no vehicles, so review had nothing to show and the
    // card appeared to open an empty screen.
    card.onclick = () => { state.jobId = job.id; show("album"); };
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
    railStat(String(totals.jobs), "Albums")
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

const PROVIDER_NAMES = {
  ollama: "Ollama", openai: "OpenAI", anthropic: "Anthropic", gemini: "Gemini",
};
const MODEL_EXAMPLES = {
  ollama: "qwen2.5vl:7b", openai: "gpt-4o",
  anthropic: "claude-sonnet-5", gemini: "gemini-2.0-flash",
};

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
      "Turning this off makes a scan several times faster."],
    ["vlm_provider", "Provider", "select", ["ollama", "openai", "anthropic", "gemini"],
      "Ollama runs locally and sends nothing off the machine. The others send crops to that provider and need a key."],
    // What a provider wants differs, so the fields below follow the choice
    // rather than listing all of them and leaving it to be worked out:
    // asking for an Ollama address while set to Gemini is a question with
    // no right answer.
    ["vlm_model", "Model", "text", null,
      (s) => `The name ${PROVIDER_NAMES[s.vlm_provider] || "the provider"} expects, `
           + `e.g. ${MODEL_EXAMPLES[s.vlm_provider] || "qwen2.5vl:7b"}.`],
    ["vlm_host", "Ollama address", "text", null, "Where Ollama is listening.",
      (s) => s.vlm_provider === "ollama"],
    ["vlm_api_key", "API key", "password", null,
      (s) => `Your ${PROVIDER_NAMES[s.vlm_provider]} key. Kept locally, in settings.json.`,
      (s) => s.vlm_provider && s.vlm_provider !== "ollama"],
    // Anthropic takes the two kinds of credential on two different headers,
    // and a good key sent on the wrong one comes back 401 looking like a
    // bad key -- so it is asked rather than guessed.
    ["anthropic_key_kind", "Key type", "select", ["auto", "api-key", "claude-code"],
      "auto reads it off the key. api-key is a console.anthropic.com key (sent as x-api-key); claude-code is a Claude Code token (sent as a bearer token).",
      (s) => s.vlm_provider === "anthropic"],
    ["vlm_input_edge", "Input size (px)", "number", null,
     "How large a crop is sent. Ollama re-sizes it to its own grid, so on a "
     + "local model anything from 512 to 1568 arrives identical — measured at "
     + "1,094 tokens for all three, and only 2048 sends more. Raising this "
     + "below 2048 costs nothing and gains nothing there. It does change what "
     + "a cloud provider sees."],
    ["identify_team", "Read team and sponsors", "bool", null, ""],
  ]],
  ["Window", [
    ["close_to_tray", "Keep running when the window is closed", "bool", null,
      "Conrod stays in the notification area and any scan carries on. Quit it from there."],
  ]],
  ["Cull verdict", [
    ["auto_reject_below_stars", "Auto-reject below", "choice",
      [[0, "Never"], [2, "2 stars"], [3, "3 stars"], [4, "4 stars"], [5, "5 stars"]],
      "Rejected as the scan finds them, before anything is spent identifying them. Nothing is deleted and nothing is written to your files — the Rejected view puts any of it back."],
    ["write_rating", "Write the star rating", "bool", null,
      "Stars follow how sharp the vehicle is, not the whole frame."],
    ["overwrite_rating", "Replace ratings I have already given", "bool", null,
      "Off, Conrod only rates frames left unrated. A camera writes 0 for unrated, which counts as unrated."],
    ["write_label", "Write the colour label", "bool", null,
      "Green kept, Yellow borderline, Red culled."],
    ["overwrite_label", "Replace labels I have already given", "bool", null,
      "Off, a frame that already carries any colour keeps it -- so on a shoot you have culled once, Conrod's colours will not appear."],
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
  const conditional = [];
  const refresh = () => {
    for (const row of conditional) {
      row.node.hidden = !row.when(settingsCache);
      if (row.hintNode && row.hint instanceof Function) {
        row.hintNode.textContent = row.hint(settingsCache);
      }
    }
  };

  form.replaceChildren(...SETTING_GROUPS.map(([title, rows]) => {
    const group = el("div", { className: "setting-group" }, el("h3", { textContent: title }));
    for (const [key, label, kind, options, hint, when] of rows) {
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
        input.onchange = () => { settingsCache[key] = input.value; refresh(); };
      } else if (kind === "choice") {
        // A select whose options read as words but store something else --
        // "2 stars" is what the photographer is choosing, 2 is what the
        // setting holds. The plain "select" above stores the label, which is
        // right when the two are the same thing and wrong here.
        input = el("select");
        for (const [val, text] of options) {
          input.append(el("option", { value: String(val), textContent: text,
                                      selected: val === value }));
        }
        input.onchange = () => {
          settingsCache[key] = typeof options[0][0] === "number"
            ? Number(input.value) : input.value;
          refresh();
        };
      } else if (kind === "number") {
        input = el("input", { type: "number", value: String(value ?? ""), step: "any" });
        input.onchange = () => { settingsCache[key] = Number(input.value); };
      } else if (kind === "password") {
        input = el("input", { type: "password", value: value ?? "", autocomplete: "off" });
        input.onchange = () => { settingsCache[key] = input.value; };
      } else {
        input = el("input", { type: "text", value: value ?? "" });
        input.onchange = () => { settingsCache[key] = input.value; };
      }
      const hintNode = hint
        ? el("div", { className: "hint",
                      textContent: hint instanceof Function ? hint(settingsCache) : hint })
        : null;
      const row = el("div", { className: "setting" },
        el("div", {}, el("div", { className: "label", textContent: label }), hintNode),
        input);
      if (when || hint instanceof Function) {
        conditional.push({ node: row, hint, hintNode, when: when || (() => true) });
      }
      group.append(row);
    }
    return group;
  }));
  refresh();

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
    // The close guard reads this without being able to ask the server.
    state.closeToTray = payload.close_to_tray;
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

// Fit the star ratings to the ones the photographer has actually given.
// Reports agreement on ratings the fit was not shown, because agreement with
// the frames it trained on is a flattering number that means nothing.
$("#btn-learn").onclick = async () => {
  const button = $("#btn-learn");
  const was = button.textContent;
  button.disabled = true;
  button.textContent = "Learning…";
  try {
    const out = await api("/api/taste", { method: "POST" });
    toast(`Learned from ${out.n} of your ratings — agrees within one star `
          + `${Math.round(out.within_one * 100)}% of the time`);
  } catch (err) {
    toast(err.message);
  } finally {
    button.disabled = false;
    button.textContent = was;
  }
};

// Forget what the model said and nothing else. The case this is for: the
// vision model was misconfigured or down, so every car came back unnamed.
// Identify only looks at cars it has never answered for, which means an
// album full of empty answers is finished as far as it is concerned.
$("#btn-reset-identifications").onclick = async () => {
  const note = $("#reset-note");
  if (!confirm(
      "Forget every make, model, colour and team Conrod read, so the album "
      + "can be identified again?\n\nThe cars it found, your star ratings, "
      + "the plates and the race numbers all stay.\n\nYour photographs are "
      + "not touched.")) return;
  try {
    const out = await api("/api/reset/identifications", { method: "POST" });
    note.textContent = "";
    toast(`${out.identifications_cleared} identifications cleared. `
          + "Run Identify Album again.");
    loadHome();
    show("home");
  } catch (err) {
    note.textContent = String(err.message || err);
  }
};

// Throw away the found cars, keep the albums. The common case by far: a
// setting changed, or the identification was wrong, and the answer is to run
// it again rather than to re-read two thousand RAWs first.
$("#btn-reset-detections").onclick = async () => {
  const note = $("#reset-note");
  let found = 0;
  try {
    found = (await api("/api/jobs")).reduce(
      (n, j) => n + (j.detection_count || 0), 0);
  } catch {
    // Counting is a courtesy. If it fails the warning still stands.
  }
  if (!confirm(
      `Throw away ${found || "all"} detected vehicles and everything read `
      + "off them?\n\nThe albums stay indexed, so scanning again does not "
      + "re-read your photographs.\n\nYour photographs are not touched.")) return;
  try {
    const out = await api("/api/reset/detections", { method: "POST" });
    note.textContent = "";
    toast(`${out.detections_removed} detections cleared.`);
    loadHome();
    show("home");
  } catch (err) {
    note.textContent = String(err.message || err);
  }
};

// The one irreversible thing in the app, so it says what will go and what
// will not before it asks, and names the count rather than saying "all".
$("#btn-reset-all").onclick = async () => {
  const note = $("#reset-note");
  let scans = 0, found = 0;
  try {
    const jobs = await api("/api/jobs");
    scans = jobs.length;
    found = jobs.reduce((n, j) => n + (j.detection_count || 0), 0);
  } catch {
    // Counting is a courtesy. If it fails the warning still stands.
  }
  if (!scans) { note.textContent = "Nothing to reset."; return; }
  if (!confirm(
      `Forget ${scans} scan${scans === 1 ? "" : "s"}`
      + `${found ? ` and ${found} identified vehicles` : ""}?\n\n`
      + "Every detection, plate, number and identification goes with them, "
      + "and they cannot be brought back without scanning again.\n\n"
      + "Your photographs are not touched.")) return;
  try {
    const out = await api("/api/reset", { method: "POST" });
    note.textContent = "";
    toast(`Reset. ${out.scans_removed} scan`
          + `${out.scans_removed === 1 ? "" : "s"} forgotten.`);
    loadHome();
    show("home");
  } catch (err) {
    // A scan in flight is the expected refusal, and it is worth reading.
    note.textContent = String(err.message || err);
  }
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
    // The folder icon is drawn by CSS (#browse-list li::before).
    const li = el("li", { textContent: name, title: dir });
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

/* Adding a folder and committing a night of GPU time to it are two different
   decisions, and they used to be one button. "Add album" indexes: it finds
   the frames, reads their EXIF and extracts the previews, which is minutes.
   Culling and identifying are then chosen per album, from the album itself. */
async function startScan(stage) {
  const path = $("#scan-path").value.trim();
  if (!path) { toast("Choose a folder first"); return; }
  state.lastStage = stage;
  try {
    const res = await api("/api/scan", {
      method: "POST",
      body: JSON.stringify({
        path, label: $("#scan-label").value.trim() || null,
        recursive: $("#scan-recursive").checked, stage,
      }),
    });
    state.watchWanted = $("#scan-watch").checked
      ? { path, recursive: $("#scan-recursive").checked } : null;
    // Added alongside a scan that is already running. The run on screen is
    // not this one, so it keeps its own progress and its own stop button --
    // this just gets a line saying it is being read.
    if (res.indexing) {
      toast("Adding the album while the scan carries on");
      openNewScan(false);
      pollScan();
      return;
    }
    openNewScan(false);
    show("scan");
    $("#scanner").hidden = false;
    showScanIdle();
    $("#btn-scan-review").hidden = true;
    $("#btn-stop").hidden = false;
    $("#btn-pause").hidden = false;
    pollScan();
  } catch (err) {
    toast(err.message);
  }
}

$("#btn-add").onclick = () => startScan("index");
$("#btn-scan").onclick = () => startScan("all");

/* Running one stage over an album that already exists. The folder is the
   album's own, so a card never has to be told where its frames are. */
async function runStage(job, stage) {
  state.lastStage = stage;
  state.jobId = job.id;
  try {
    await api("/api/scan", {
      method: "POST",
      body: JSON.stringify({ path: job.root, label: job.label, recursive: true,
                             resume_job: job.id, stage }),
    });
    show("scan");
    $("#scanner").hidden = false;
    showScanIdle();
    $("#btn-scan-review").hidden = true;
    $("#btn-stop").hidden = false;
    $("#btn-pause").hidden = false;
    pollScan();
  } catch (err) { toast(err.message); }
}

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

$("#btn-scan-review").onclick = () =>
  show(state.lastStage === "index" ? "album" : "review");

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

function etaText(seconds) {
  if (!seconds || seconds < 0) return "";
  if (seconds < 90) return `, ${Math.round(seconds)}s left`;
  return `, ${Math.round(seconds / 60)} min left`;
}

async function pollUpdate() {
  try {
    const u = await api("/api/update/status");
    const pct = u.total ? (u.done / u.total) * 100 : 0;
    $("#update-fill").style.width = `${pct}%`;
    $("#update-status").textContent = u.total
      ? `${u.message} — ${(u.done / 1e6).toFixed(0)} of ${(u.total / 1e6).toFixed(0)} MB`
        + (u.rate ? ` at ${(u.rate / 1e6).toFixed(1)} MB/s${etaText(u.eta)}` : "")
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
    // Closing the window ends the run, so the guard below needs to know.
    state.scanning = Boolean(h.scanning) && !h.paused;
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

// Closing the window closes the application, which ends the scan wherever it
// had got to. Hours of a shoot went that way on a misplaced click, and
// nothing asked. The browser only shows this prompt on a page the user has
// actually interacted with -- true of an app window by the time a scan is
// running -- and the wording is Chromium's own, not ours.
window.addEventListener("beforeunload", (event) => {
  // Closing the window used to end the program and throw the scan away, so
  // it was worth interrupting for. With the tray on it costs nothing -- the
  // scan carries on -- and a warning that cries wolf is worse than none.
  if (!state.scanning || state.closeToTray) return;
  event.preventDefault();
  event.returnValue = "";        // still required by Chromium
  return "";                     // and by older WebKit
});

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
    renderIndexing(data.indexing);
    showScanIdle();
    reflectRunning(data.active);
    // A watch continues an album, and the album does not exist until the
    // pipeline has created it -- so it can only be armed once the scan has
    // reported which job it is filling.
    if (state.watchWanted && data.job_id) armWatch(data.job_id);

    // An album added while a scan runs finishes on its own clock. Its own
    // completion is worth saying; the scan it was added alongside is not
    // over and must not be reported as though it were.
    const indexing = data.indexing || {};
    if (state.indexingWas && !indexing.active) {
      state.indexingWas = false;
      if (indexing.error) toast(indexing.error);
      else {
        toast(indexing.message || "Album added");
        if (indexing.job_id) { state.jobId = indexing.job_id; show("album"); }
      }
    }
    state.indexingWas = Boolean(indexing.active);

    if (!data.active && !indexing.active) {
      clearInterval(state.scanTimer);
      $("#btn-stop").hidden = true;
      $("#btn-pause").hidden = true;
      // Only the run that owns this screen gets the finished-run treatment.
      // An album indexed alongside it has already had its say above.
      if (state.lastStage && !$("#scanner").hidden) {
        $("#btn-scan-review").hidden = false;
        if (data.error) toast(data.error);
        else toast(data.message || "Scan complete");
        if (data.job_id) state.jobId = data.job_id;
        // Reading a folder takes seconds and finds nothing — leaving someone
        // looking at a finished progress bar, with the two decisions that
        // matter on another screen. Go to the album instead.
        if (state.lastStage === "index" && data.job_id && !data.error) {
          show("album");
        }
      }
    }
  }, 500);
}

// A one-line "this folder is being read" while a scan holds the main
// progress panel. Deliberately small: it is minutes of disk work, not the
// hours of GPU time the panel above it is reporting on.
// The Scan screen is only ever the running job now, so when there is no
// job it has to say so -- an empty dark panel reads as something that
// failed to load rather than as nothing to show.
function showScanIdle() {
  const running = !$("#scanner").hidden;
  $("#scan-idle").hidden = running;
}

/* A second scan cannot start while one is running: the detector and the
   GPU are single-tenant, and the server refuses it. Saying so on the
   button is better than letting it be pressed and answering with an
   error -- adding an album still works, because indexing is neither. */
function reflectRunning(active) {
  const scanBtn = $("#btn-scan");
  if (!scanBtn) return;
  scanBtn.disabled = Boolean(active);
  scanBtn.title = active
    ? "A scan is already running. Add the album now and cull it when that one finishes."
    : "Index, cull and identify in one go, the way it used to work.";
  const note = $("#scan-busy-note");
  if (note) note.hidden = !active;
}

function renderIndexing(indexing) {
  const line = $("#indexing-line");
  if (!line) return;
  if (!indexing || !indexing.active) { line.hidden = true; return; }
  line.hidden = false;
  const done = indexing.done || 0;
  const total = indexing.total || 0;
  const where = indexing.label ? `“${indexing.label}”` : "album";
  line.textContent = total
    ? `Adding ${where} — ${done.toLocaleString()} of ${total.toLocaleString()} frames`
    : `Adding ${where} — ${indexing.message || "reading the folder"}`;
}

/* The picker lives on the home page, in the card that offers it. Pressing
   "+ New scan" opens it there rather than sending anyone to another
   screen, and the Scan screen is now only the running job.

   It was briefly a "+ Add another album" button floating under the live
   scan view: a ghost button on its own in the dark, which is no way to
   offer the main thing this application does. */
function openNewScan(open) {
  const card = $("#new-scan-card");
  const body = $("#scan-setup-body");
  body.hidden = !open;
  card.classList.toggle("open", open);
  $("#btn-new-scan").textContent = open ? "Close" : "+ New scan";
  if (open) $("#scan-path").focus();
  else $("#browser").hidden = true;
}
$("#btn-new-scan").onclick = () => openNewScan($("#scan-setup-body").hidden);

/* ── watching a folder ────────────────────────────────────── */
/* The card still copying while the shoot is packed up. The watch adds the
   frames that land afterwards to the album the scan just filled, so the
   second half of a shoot does not have to be scanned by hand. */

async function armWatch(jobId) {
  const wanted = state.watchWanted;
  state.watchWanted = null;            // one attempt; a failure is reported
  try {
    await api("/api/watch", {
      method: "POST",
      body: JSON.stringify({ active: true, job_id: jobId, ...wanted }),
    });
    pollWatch();
  } catch (err) {
    toast(`Could not watch the folder: ${err.message}`);
  }
}

async function pollWatch() {
  let data;
  try { data = await api("/api/watch"); } catch { return; }
  $("#watch-card").hidden = !data.active;
  if (data.active) {
    const added = data.added
      ? `${data.added} new frame${data.added === 1 ? "" : "s"} picked up`
      : "Watching for new frames";
    $("#watch-text").textContent = data.message && data.message !== "waiting"
      ? `${added} — ${data.message}` : added;
  }
  clearTimeout(state.watchTimer);
  if (data.active) state.watchTimer = setTimeout(pollWatch, 5000);
}

$("#btn-watch-stop").onclick = async () => {
  try {
    await api("/api/watch", { method: "POST",
                              body: JSON.stringify({ active: false }) });
    $("#watch-card").hidden = true;
    clearTimeout(state.watchTimer);
    toast("Stopped watching the folder");
  } catch (err) { toast(err.message); }
};

function renderScan(data) {
  const pause = $("#btn-pause");
  pause.dataset.paused = data.paused ? "1" : "0";
  pause.textContent = data.paused ? "Resume" : "Pause";
  pause.classList.toggle("accent", !!data.paused);

  const pct = data.total ? (data.done / data.total) * 100 : 0;
  $("#scan-fill").style.width = `${pct}%`;
  $("#scan-counter").textContent = `${data.done} / ${data.total || "?"}`;
  // Null means the estimate is not trustworthy yet. Saying so beats both a
  // blank line and a confident number derived from four frames.
  $("#scan-eta").textContent = data.eta != null
    ? `about ${formatSeconds(data.eta)} left`
    : (data.active ? "estimating…" : "");
  $("#scan-found").textContent = data.message || "";

  const live = data.live || (data.current ? [data.current] : []);
  $("#scan-inflight").textContent = live.length ? `${live.length}` : "";
  $("#scan-waiting").hidden = live.length > 0;
  if (!live.length) {
    $("#scan-frames").replaceChildren();
    return;
  }

  // Tiles are reused by token rather than rebuilt each poll. Replacing them
  // wholesale restarted every <img> load twice a second, so the photographs
  // never finished arriving and the panel strobed.
  const board = $("#scan-frames");
  const existing = new Map(
    [...board.children].map((node) => [node.dataset.token, node]));

  board.replaceChildren(...live.map((frame) => {
    const token = String(frame.token ?? data.frame_token);
    const node = existing.get(token) || frameTile(frame, token);
    updateTile(node, frame, data.active);
    return node;
  }));
}

function frameTile(frame, token) {
  const node = el("div", { className: "frame" });
  node.dataset.token = token;

  const head = el("div", { className: "frame-head" },
    el("span", { className: "name mono", textContent: frame.name || "" }),
    el("span", { className: "phase mono", textContent: "" }));

  const img = el("img", { alt: "", decoding: "async" });
  const overlay = el("div", { className: "overlay" });
  // Boxes belong to one specific photograph. It arrives over HTTP a moment
  // after the boxes do, so drawing them straight away paints this frame's
  // boxes over the previous frame's picture -- which is what made the live
  // view look like it was glitching.
  img.onload = () => { node.dataset.loaded = "1"; fitTile(node); };
  img.src = `/api/scan/frame?t=${token}`;

  const stage = el("div", { className: "frame-stage" }, img, overlay);
  node.append(head, stage, el("div", { className: "frame-log mono" }));
  return node;
}

// What to write on a box drawn over a frame in the live view.
//
// In the order the photographer would say it: the race number if one was
// read, otherwise what the car turned out to be, otherwise only what the
// detector saw. The identification was already arriving on every box and
// was never shown -- so a whole identify pass drew "car · 0%" over every
// vehicle it had just named, and the one frame where the vision model had
// failed looked exactly like the ones where it had worked.
function boxLabel(box) {
  const pct = (value) => `${Math.round((value || 0) * 100)}%`;
  if (box.number) return `#${box.number} · ${pct(box.read_conf)}`;
  if (box.title) return box.title;
  const kind = box.kind || "vehicle";
  const named = kind.charAt(0).toUpperCase() + kind.slice(1);
  // No confidence until there is one. A detector score of zero is what a
  // missing number looks like, not a detection nobody believed in.
  return box.conf ? `${named} · ${pct(box.conf)}` : named;
}

function updateTile(node, frame, active) {
  node.querySelector(".name").textContent = frame.name || "";
  node.querySelector(".phase").textContent =
    active ? (frame.phase || "SCANNING") : "DONE";
  node.classList.toggle("working", !!active && frame.phase !== "DONE");

  const overlay = node.querySelector(".overlay");
  if (node.dataset.loaded !== "1") {
    overlay.replaceChildren();
  } else {
    fitTile(node);
    overlay.replaceChildren(...(frame.boxes || []).map((box) => {
      const mark = el("div", { className: "box" + (box.number ? "" : " pending") });
      mark.style.left = `${box.x * 100}%`;
      mark.style.top = `${box.y * 100}%`;
      mark.style.width = `${box.w * 100}%`;
      mark.style.height = `${box.h * 100}%`;
      mark.append(el("div", { className: "box-label", textContent: boxLabel(box) }));
      return mark;
    }));
  }

  const log = node.querySelector(".frame-log");
  log.replaceChildren(...(frame.log || []).slice(-3).map((line) =>
    el("div", { textContent: line })));
}

// The photo is letterboxed inside the tile by object-fit: contain, but the
// overlay is pinned to the tile. Box coordinates are fractions of the
// *photo*, so on any frame whose aspect does not match the tile the boxes
// drifted sideways and ran off the edge. Size the overlay to the pixels the
// image actually occupies.
function fitTile(node) {
  const img = node.querySelector("img");
  const overlay = node.querySelector(".overlay");
  const nw = img.naturalWidth, nh = img.naturalHeight;
  if (!nw || !nh) return;

  const boxW = img.clientWidth, boxH = img.clientHeight;
  if (!boxW || !boxH) return;
  const scale = Math.min(boxW / nw, boxH / nh);
  const drawnW = nw * scale, drawnH = nh * scale;

  overlay.style.left = `${img.offsetLeft + (boxW - drawnW) / 2}px`;
  overlay.style.top = `${img.offsetTop + (boxH - drawnH) / 2}px`;
  overlay.style.width = `${drawnW}px`;
  overlay.style.height = `${drawnH}px`;
}

window.addEventListener("resize", () => {
  document.querySelectorAll(".frame").forEach(fitTile);
});

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
    $("#empty").textContent = "No albums yet. Run a scan from the Scan tab.";
    $("#grid").replaceChildren();
    return;
  }
  const select = $("#job-select");
  select.replaceChildren(...jobs.map((j) =>
    el("option", { value: j.id, textContent: `${j.label || j.root} — ${j.image_count}` })));
  if (!state.jobId || !jobs.some((j) => j.id === state.jobId)) state.jobId = jobs[0].id;
  select.value = String(state.jobId);

  // Grouping is the last stage of a scan, so a job still running shows every
  // frame on its own -- ten frames of one Jaguar as ten cards, and two frames
  // sharing a 94% plate read still side by side. That looks exactly like
  // grouping having run and failed, so say which it is.
  const job = jobs.find((j) => j.id === state.jobId) || {};
  const note = $("#review-note");
  const left = job.unfinished_count || 0;
  // A job's status stays "scanning" until something finishes it, so a cull
  // that ran to the end still says so with nothing left to do. Counting the
  // frames rather than trusting the status stopped this reading "Still
  // scanning — 0 frames to go" indefinitely.
  // Whether anything is actually running right now. Without asking, an
  // album that stopped part way through read "Still scanning — 2,674 frames
  // to go" for ever: the count was right and the tense was wrong, so the
  // honest answer to "it stopped and said nothing" was sitting on the
  // screen telling the photographer to keep waiting.
  let running = false;
  try {
    running = Boolean((await api("/api/scan")).active);
  } catch {
    // Not knowing is not worth failing the screen over.
  }

  if (left > 0 && running) {
    note.hidden = false;
    note.textContent = `Still scanning — ${left.toLocaleString()} frame`
      + `${left === 1 ? "" : "s"} to go. Vehicles are grouped once the scan `
      + `finishes, so every frame is listed on its own until then.`;
  } else if (left > 0) {
    note.hidden = false;
    note.replaceChildren(
      el("span", { textContent:
        `This album stopped with ${left.toLocaleString()} frame`
        + `${left === 1 ? "" : "s"} never looked at`
        + (job.failed_count
            ? `, and ${job.failed_count.toLocaleString()} that could not be read`
            : "")
        + ". Carrying on picks up where it left off — the frames already "
        + "done are not read again. " }),
      el("button", { className: "step", textContent: "Carry on",
                     onclick: () => runStage(job, state.lastStage || "cull") }),
    );
  } else if (job.status === "scanning" && !job.grouped_count) {
    // Everything has been looked at, but grouping is the last step of a full
    // scan and this album has not had it. Say what to press rather than
    // leaving every frame of one car listed as a separate vehicle.
    note.hidden = false;
    note.textContent = "Every frame is listed on its own because this album "
      + "has not been grouped yet. Press Regroup to gather the frames of each "
      + "vehicle together.";
  } else {
    note.hidden = true;
  }

  state.offset = 0;
  state.stack = null;
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
    const li = el("li", {},
      el("span", { className: "n", textContent: `#${n.number}` }),
      el("span", { className: "who", textContent: n.who || "" }),
      el("span", { className: "c", textContent: n.frames }));
    li.dataset.value = n.number;
    li.onclick = () => pickFacet("number", n.number);
    return li;
  }));

  $("#plate-list").replaceChildren(...(data.plates || []).map((p) => {
    const li = el("li", {},
      el("span", { className: "n", textContent: p.plate }),
      el("span", { className: "c", textContent: p.frames }));
    li.dataset.value = p.plate;
    li.onclick = () => pickFacet("plate", p.plate);
    return li;
  }));

  $("#facet-empty").hidden = data.numbers.length || (data.plates || []).length;
  $("#facet-empty").textContent = "Nothing read off this album yet.";
  renderFacets();
}

function stat(label, value) {
  return el("span", {}, el("b", { textContent: String(value ?? 0) }), ` ${label}`);
}

async function loadGrid(append = false) {
  // "By number" with nothing chosen yet is a valid state: show the sidebar
  // and wait for a pick rather than asking the server for an impossible view.
  const params = new URLSearchParams({
    view: state.view, limit: state.limit, offset: state.offset,
    sort: state.sort,
  });
  // A sidebar pick narrows whichever tab is showing. The server's number
  // and plate views are what actually filter, so a pick selects that view
  // and the tab decides whether rejected ones come with it.
  if (state.facetPick) {
    params.set("view", state.facetPick.kind);
    params.set(state.facetPick.kind, state.facetPick.value);
  }
  if (state.search) params.set("search", state.search);
  if (state.minStars) params.set("min_stars", state.minStars);

  const data = await api(`/api/jobs/${state.jobId}/detections?${params}`);
  state.total = data.total;
  state.items = append ? [...(state.items || []), ...data.items] : data.items;
  // Loading more while a stack is open would drop the person back out to
  // the wall of stacks mid-scroll, so the open stack survives the append.
  renderGrid();

  renderFoot();
  $("#empty").hidden = state.total > 0;
  // setTimeout, not requestAnimationFrame: rAF is tied to painting, and
  // in a pane that is not currently painting it never runs, so the feed
  // would quietly stop feeding.
  setTimeout(() => maybeLoadMore(), 0);
  if (!state.total) {
    $("#empty").textContent = state.view === "review"
      ? "Nothing left to review. Write the XMP when you are ready."
      : "Nothing here.";
  }
}

/* ── album ────────────────────────────────────────────────────
   An album and the two things you might do to it. Adding a folder used to
   drop you on a progress bar, after which Cull and Identify were small
   buttons on a card back on the home screen — the work made to look like
   an afterthought to the reading of the folder, which is the cheap part. */

const SHEET_PAGE = 200;

async function loadAlbum() {
  const id = state.jobId;
  if (!id) { show("home"); return; }
  state.sheetOffset = 0;
  const jobs = await api("/api/jobs");
  const job = jobs.find((j) => j.id === id);
  if (!job) { toast("That album is gone"); show("home"); return; }
  state.album = job;

  $("#album-name").textContent = job.label || job.root;
  $("#album-path").textContent = job.root;
  $("#album-stat").textContent = `${job.image_count || 0}`;
  $("#album-state").textContent = ALBUM_STATE[job.status] || job.status || "";

  const found = job.detection_count || 0;
  $("#album-grid").replaceChildren(
    stat("Vehicles", found),
    stat("Frames", job.image_count || 0));

  // Offer only what this album is actually ready for. An album with no
  // vehicles cannot be identified, and offering it anyway means pressing a
  // button that does nothing and says nothing about why.
  // Culling is optional. An album that has only been indexed can go straight
  // to being identified -- it finds the cars on the way and keeps all of
  // them -- so this is offered whenever there is anything to work on at all.
  // It used to be greyed out until a cull had run, which made "keep
  // everything and name it" a thing the app could not do.
  const empty = !found && job.status !== "indexed";
  offer($("#do-cull"), true,
        () => runStage(job, "cull"));
  offer($("#do-identify"), !empty,
        () => runStage(job, "identify"),
        empty ? "Nothing to identify yet — index the album first." : "");
  offer($("#do-both"), true, () => runStage(job, "all"));

  $("#album-review").disabled = !found;
  $("#album-write").disabled = !found;
  $("#album-hint").textContent = found
    ? "Nothing is written to your photographs until you press Write XMP."
    : "No vehicles found yet.";

  await loadSheet(true);
}

const ALBUM_STATE = {
  indexed: "frames read — ready to cull",
  culled: "culled — ready to identify",
  scanning: "still working",
  done: "finished",
};

function stat(label, n) {
  return el("div", {}, el("div", { className: "n", textContent: String(n) }),
                       el("div", { className: "big-label", textContent: label }));
}

function offer(card, enabled, run, why) {
  card.setAttribute("aria-disabled", enabled ? "false" : "true");
  card.title = enabled ? "" : (why || "");
  card.onclick = enabled ? run : null;
}

async function loadSheet(reset) {
  const id = state.jobId;
  const data = await api(
    `/api/jobs/${id}/frames?offset=${state.sheetOffset}&limit=${SHEET_PAGE}`);
  const tiles = data.frames.map((f) => {
    const tile = el("div", { className: "tile" });
    if (f.viewable) {
      tile.append(el("img", { src: `/api/thumb/${f.id}`, alt: "",
                              loading: "lazy", decoding: "async" }));
    } else {
      tile.append(el("div", { className: "blank",
                              textContent: f.status === "pending"
                                ? "not read yet" : "no preview" }));
    }
    if (f.vehicles) {
      tile.append(el("span", { className: "count",
                               textContent: `${f.kept}/${f.vehicles}` }));
    }
    if (f.verdict) {
      tile.append(el("span", { className: `pip ${f.verdict}`,
                               title: `${f.verdict} — ${f.label || ""}` }));
    }
    tile.append(el("div", { className: "cap", textContent: f.name,
                            title: f.name }));
    return tile;
  });

  const sheet = $("#album-sheet");
  if (reset) sheet.replaceChildren(...tiles); else sheet.append(...tiles);
  $("#album-count").textContent = `${sheet.children.length} of ${data.total}`;
  $("#album-empty").hidden = data.total > 0;
  $("#album-more").hidden = sheet.children.length >= data.total;
}

$("#album-more").onclick = async () => {
  state.sheetOffset += SHEET_PAGE;
  await loadSheet(false);
};
$("#album-review").onclick = () => show("review");
$("#album-write").onclick = () => { show("review"); toast("Review, then Write XMP"); };

// One vehicle, however many frames it appeared in.
//
// A frame nothing has been read off yet is not a vehicle of its own -- it
// used to become a one-card "group" with an empty header, and a scan that
// had not finished identifying produced a screen of hundreds of them, each
// claiming to be a distinct car. They all go to one Unknown stack instead,
// which is somewhere to go and correct them from rather than noise to
// scroll past.
const UNKNOWN = "unknown";

function groupItems(items) {
  const order = [];
  const byKey = new Map();
  for (const item of items) {
    const named = identified(item.attributes) || item.number || item.plate;
    const key = !named ? UNKNOWN
      : item.group_size > 1 && item.group_key != null
        ? `g${item.group_key}` : `d${item.id}`;
    if (!byKey.has(key)) { byKey.set(key, []); order.push(key); }
    byKey.get(key).push(item);
  }
  // Unknown last: it is the leftovers, not the first thing worth looking at.
  order.sort((a, b) => (a === UNKNOWN) - (b === UNKNOWN));
  return order.map((key) => ({ key, members: byKey.get(key) }));
}

// "<plate> - <team> - Red Toyota Celica", with the parts that were never
// read left out rather than shown as empty dashes.
function stackName(key, members) {
  if (key === UNKNOWN) return "Unknown";
  const attrs = members.find((m) => identified(m.attributes))?.attributes || {};
  const number = members.find((m) => m.number)?.number;
  const plate = members.find((m) => m.plate)?.plate;
  const lead = members[0] || {};
  // The title already carries the competition number -- VehicleAnalysis.title
  // puts "#21" on the front of it -- so prepending it here as well printed
  // "#21 · Nosse · #21 Black Mini Cooper".
  const name = String(lead.title || lead.cls || "Vehicle").replace(/^#\S+\s+/, "");
  const bits = [];
  if (number) bits.push(`#${number}`);
  if (plate) bits.push(plate);
  if (attrs.team) bits.push(attrs.team);
  else if (identified(attrs)) bits.push("Independent");
  bits.push(name);
  return bits.join(" · ");
}

// How many sponsors fit before it stops being readable. The rest are still
// reachable: they go behind a "+N" that lists all of them.
const SPONSORS_SHOWN = 6;

// The server always sends a full attributes object, every field present but
// null until identify() has actually read something -- so "does this object
// have any keys" can no longer tell an identified frame from one still
// waiting its turn. This checks for content instead of presence.
function identified(attrs) {
  return !!(attrs && (attrs.make || attrs.colour || attrs.body_type || attrs.team
    || (attrs.sponsors && attrs.sponsors.length)
    || (attrs.livery_text && attrs.livery_text.length)));
}

/* ── stacks and the gallery ────────────────────────────────────
   The review page is a wall of stacks, one per vehicle: the best frame of
   it face up, the rest of the pile showing behind. Clicking a stack opens
   that vehicle's own gallery in the same place, with a back button.

   It used to lay every frame of every vehicle out at once, which meant a
   shoot with eighty cars in it was a page you scrolled for a minute to
   find one. A stack says "this is one car, here is what it looks like,
   there are twelve of them" in the space the old header alone took. */

function stackTile(group) {
  const { key, members } = group;
  const lead = bestFrame(members);
  const tile = el("div", { className: "stack" + (key === UNKNOWN ? " unknown" : "") });
  tile.dataset.key = key;

  // Two empty boxes behind the photo, so a pile reads as a pile at a
  // glance. Only drawn when there is actually more than one frame --
  // a stack of one that looks like a stack of six is a lie about the shoot.
  const pile = el("div", { className: "pile" + (members.length > 1 ? " deep" : "") });
  const thumb = el("img", { className: "stack-shot", src: `${lead.crop_url}?w=420`,
                            loading: "lazy", decoding: "async",
                            alt: stackName(key, members) });
  pile.append(thumb);
  if (members.length > 1) {
    pile.append(el("span", { className: "stack-count",
                             textContent: String(members.length) }));
  }

  const cap = el("div", { className: "stack-cap" });
  if (lead.colour_hex && key !== UNKNOWN) {
    const swatch = el("span", { className: "swatch" });
    swatch.style.background = lead.colour_hex;
    cap.append(swatch, " ");
  }
  cap.append(el("span", { className: "stack-name",
                          textContent: stackName(key, members) }));

  const sub = el("div", { className: "stack-sub" });
  if (key === UNKNOWN) {
    sub.textContent = `${members.length} frame${members.length === 1 ? "" : "s"}`
      + " nothing was read off";
  } else {
    const pct = Math.round((lead.group_agreement || 0) * 100);
    sub.textContent = members.length > 1
      ? `${members.length} frames · ${pct}% agree`
      : "1 frame";
  }

  tile.append(pile, cap, sub);
  tile.onclick = () => openStack(key);
  return tile;
}

// The frame shown face-up on the stack: the best-rated one, because a pile
// should be represented by its keeper, not by whichever came out of SQLite
// first -- often a blurred lead-in shot of the same car.
function bestFrame(members) {
  return members.reduce((best, m) => {
    const rank = (x) => (x.stars || 0) * 10 + (x.rating || 0) - (x.rejected ? 100 : 0);
    return rank(m) > rank(best) ? m : best;
  }, members[0]);
}

function openStack(key) {
  state.stack = key;
  setCursor(null);
  renderGrid();
  $("#grid-wrap").scrollTop = 0;
}

function closeStack() {
  state.stack = null;
  setCursor(null);
  renderGrid();
}

// Everything the last load returned, kept so opening and closing a stack
// does not have to go back to the server for frames it already has.
function renderGrid() {
  const groups = groupItems(state.items || []);
  const grid = $("#grid");
  if (!state.stack) {
    grid.className = "stacks";
    grid.replaceChildren(...groups.map(stackTile));
    return;
  }
  const group = groups.find((g) => g.key === state.stack);
  if (!group) { closeStack(); return; }
  grid.className = "gallery";
  grid.replaceChildren(galleryHead(group), ...group.members.map(galleryCard));
}

function galleryHead(group) {
  const head = el("div", { className: "gallery-head" });
  const back = el("button", { className: "ghost back", textContent: "←",
                              title: "Back to every vehicle (Esc)" });
  back.onclick = closeStack;
  head.append(back, el("h3", { textContent: stackName(group.key, group.members) }));

  if (group.key === UNKNOWN) {
    head.append(el("span", {
      className: "fact soft",
      textContent: "correct one and it moves to its own stack",
    }));
  }
  return head;
}

function shortCamera(name) {
  // "Canon EOS R5 123456789" is too long for a chip and the serial is the
  // part that does not read as a camera. Keep the model, mark the body.
  const bits = String(name).split(" ");
  const tail = bits[bits.length - 1];
  return /^\d{4,}$/.test(tail)
    ? `${bits.slice(0, -1).join(" ")} · ${tail.slice(-4)}`
    : String(name);
}

function band(value) {
  if (value == null) return "low";
  if (value >= 0.85) return "high";
  if (value >= 0.6) return "mid";
  return "low";
}

// What a card shows about its own frame: the picture, the rating, whether
// it was read, and the two fields worth correcting by hand. Everything
// about the *vehicle* -- its name, team, sponsors, camera, how many frames
// agree -- is said once on the group header above the strip, because every
// card in a group used to repeat all of it, and a twelve-frame vehicle
// meant reading "blue Mini Cooper S, Nosso Racing, Canon EOS R7" twelve
// times to find the one thing that actually differs frame to frame: the
// picture.
/* A field edited the way an email client edits recipients: each value is a
   rectangle with an x, and a + opens a box to type the next one. The team
   and the livery are read off the car by a model that is often close but
   rarely exactly right, and correcting "Nosse" to "Nosso" used to mean
   there was nowhere to do it -- they were plain text on the card. */
function chipField({ values, single, placeholder, onChange }) {
  const box = el("div", { className: "chips-edit" });

  const commit = (next) => {
    const seen = new Set();
    const cleaned = [];
    for (const v of next) {
      const text = String(v).trim();
      if (text && !seen.has(text.toUpperCase())) {
        seen.add(text.toUpperCase());
        cleaned.push(text);
      }
    }
    onChange(cleaned);
    draw(cleaned);
  };

  function draw(list) {
    const kids = list.map((value) => {
      const chip = el("span", { className: "chip", textContent: value });
      const x = el("button", { className: "chip-x", textContent: "×",
                               title: `Remove ${value}` });
      x.onclick = (e) => {
        e.stopPropagation();
        commit(list.filter((v) => v !== value));
      };
      chip.append(x);
      return chip;
    });

    // One team, many sponsors: a single-valued field offers the + only
    // while it is empty, so there is never a second team to disagree with.
    if (!single || !list.length) {
      const add = el("button", { className: "chip-add", textContent: "+",
                                 title: placeholder });
      add.onclick = (e) => {
        e.stopPropagation();
        const input = el("input", { className: "chip-input", placeholder });
        add.replaceWith(input);
        input.focus();
        let done = false;
        const finish = (keep) => {
          if (done) return;
          done = true;
          const text = input.value.trim();
          if (keep && text) commit([...list, text]);
          else draw(list);
        };
        input.onkeydown = (ev) => {
          if (ev.key === "Enter") { ev.preventDefault(); finish(true); }
          else if (ev.key === "Escape") { ev.preventDefault(); finish(false); }
        };
        input.onblur = () => finish(true);
      };
      kids.push(add);
    }
    box.replaceChildren(...kids);
  }

  draw(values);
  return box;
}

// One photograph inside a vehicle's gallery: the picture, what was read off
// it, and the two judgements worth making frame by frame -- how many stars,
// and whether it stays at all.
function galleryCard(item) {
  const attrs = item.attributes || {};
  const cutByHand = item.rejected && !item.cull_reason;
  const node = el("div", { className: "card" + (cutByHand ? " rejected" : "")
                                              + (item.cull_reason ? " culled" : "") });
  node.dataset.id = item.id;
  node._item = item;      // so the full-frame view can render any card
  node.addEventListener("mousedown", () => setCursor(node, { scroll: false }));

  const frame = el("div", { className: "frame-box" });
  const thumb = el("img", { className: "thumb", src: `${item.crop_url}?w=420`,
                            loading: "lazy", decoding: "async",
                            alt: item.title || item.cls, title: item.filename });
  thumb.onclick = (e) => {
    if (e.shiftKey) { toggleSelect(node, item.id); return; }
    openFrame(node);
  };
  frame.append(thumb);

  const verdict = item.rating_verdict || item.sharpness_verdict;
  if ((verdict && verdict !== "unknown") || item.stars) {
    const why = [`subject sharpness ${(item.sharpness || 0).toFixed(2)}`];
    if (item.background >= 0) why.push(`background ${item.background.toFixed(2)}`);
    if (item.clipped) {
      why.push(item.clipped === 1 ? "touches the frame edge"
                                  : `cut off on ${item.clipped} edges`);
    }
    if (item.sharp_end && item.sharp_end !== "even") {
      why.push(`sharpest towards the ${item.sharp_end}`);
    }
    frame.append(el("span", {
      className: "focus stars " + (verdict || "") + (item.by_hand ? " by-hand" : ""),
      textContent: item.stars ? "★".repeat(item.stars) : (verdict || "—"),
      title: item.by_hand
        ? `${item.stars} stars, given by you — this is what Write XMP will use`
        : `${why.join(" · ")} — measured on the vehicle, not the whole frame`,
    }));
    if (verdict) node.classList.add(`rated-${verdict}`);
  }
  if (item.panning) {
    frame.append(el("span", { className: "focus panning", textContent: "panned",
      title: "Subject sharp against a blurred background — kept, never auto-culled" }));
  }
  if (item.cull_reason) {
    frame.append(el("div", { className: "culled", textContent: item.cull_reason }));
  }

  // What was read off this frame, on inline labelled lines -- "Plate: X",
  // not a label column and a value column, which cost four rows of card
  // height to say four short things.
  const facts = el("div", { className: "readout" });
  const readLine = (label, ...value) =>
    facts.append(el("div", { className: "read" },
      el("span", { className: "lbl", textContent: label }), ...value));

  const plateInput = el("input", { className: "plate", value: item.plate || "",
                                   placeholder: "—", autocomplete: "off" });
  const numInput = el("input", { className: "num", value: item.number || "",
                                 placeholder: "—", inputMode: "numeric",
                                 autocomplete: "off" });

  const plateConf = el("span", { className: "tag conf",
    textContent: item.plate && item.plate_conf != null
      ? `${Math.round(item.plate_conf * 100)}%` : "" });
  plateConf.hidden = !item.plate;
  plateConf.dataset.band = item.plate ? band(item.plate_conf) : "none";

  const numConf = el("span", { className: "tag conf",
    textContent: item.number && item.number_conf != null
      ? `${Math.round(item.number_conf * 100)}%` : "" });
  numConf.hidden = !item.number;
  numConf.dataset.band = item.number ? band(item.number_conf) : "none";

  readLine("Plate", plateInput, plateConf);
  readLine("No.", numInput, numConf);

  // Team and livery are read off the car by a model that is often close and
  // rarely exact -- "Nosse" for "Nosso" -- and until now there was nowhere
  // to correct either: they were plain text printed on the card.
  const saveAttrs = (patch) => api(`/api/detections/${item.id}`, {
    method: "POST",
    body: JSON.stringify({ attributes: patch, reviewed: true }),
  }).then((saved) => {
    node.classList.add("saved");
    loadSummary();
    return saved;
  }).catch((err) => toast(err.message));

  const teamRow = el("div", { className: "read" },
    el("span", { className: "lbl", textContent: "Team" }));
  teamRow.append(chipField({
    values: attrs.team ? [attrs.team] : [],
    single: true,
    placeholder: "team or entrant",
    onChange: (list) => {
      attrs.team = list[0] || null;
      saveAttrs({ team: list[0] || "" });
    },
  }));
  if (!attrs.team) {
    teamRow.append(el("span", { className: "val muted-note",
      textContent: identified(attrs) ? "Independent" : "",
      title: "No team name was read -- a privateer, or none shown" }));
  }
  facts.append(teamRow);

  const liveryRow = el("div", { className: "read" },
    el("span", { className: "lbl", textContent: "Livery" }));
  liveryRow.append(chipField({
    values: (attrs.sponsors || []).filter((s) => s && s !== attrs.team),
    placeholder: "sponsor",
    onChange: (list) => {
      attrs.sponsors = list;
      saveAttrs({ sponsors: list });
    },
  }));
  facts.append(liveryRow);

  if (item.who) {
    readLine("Driver", el("span", { className: "val", textContent: item.who }));
  }

  const save = async (patch) => {
    const saved = await api(`/api/detections/${item.id}`, {
      method: "POST", body: JSON.stringify({ ...patch, reviewed: true }),
    });
    node.classList.add("saved");
    loadSummary();
    return saved;
  };
  numInput.onkeydown = async (e) => {
    if (e.key === "Enter") { e.preventDefault(); await save({ number: numInput.value.trim() }); }
  };
  numInput.onblur = () => {
    if (numInput.value.trim() !== (item.number || "")) save({ number: numInput.value.trim() });
  };
  plateInput.onblur = () => {
    if (plateInput.value.trim() !== (item.plate || "")) save({ plate: plateInput.value.trim() });
  };

  // The rating, as five stars to click rather than a number to remember a
  // shortcut for. The keyboard still does it faster; this is for the pass
  // where a mouse is already in your hand.
  const starRow = el("div", { className: "stars-control" });
  const paintStars = (n) => {
    [...starRow.querySelectorAll(".star")].forEach((s, i) => {
      s.classList.toggle("on", i < n);
    });
  };
  for (let n = 1; n <= 5; n += 1) {
    const s = el("button", { className: "star", textContent: "★",
                             title: `${n} star${n === 1 ? "" : "s"}` });
    s.onclick = async (e) => {
      e.stopPropagation();
      setCursor(node, { scroll: false });
      await setStars(node, Number(node.dataset.stars) === n ? 0 : n);
      paintStars(Number(node.dataset.stars) || 0);
    };
    starRow.append(s);
  }
  node.dataset.stars = item.stars || "";
  paintStars(item.stars || 0);

  if (item.bystander) node.classList.add("bystander");
  const rejectBtn = el("button", { className: "ghost danger cut",
    textContent: cutLabel(item, node),
    title: (item.in_frame || 1) > 1
      ? "Several vehicles in this frame, so this takes this one out of the "
        + "vehicle and leaves the photograph alone (X)."
      : "Reject this vehicle (X). Click again to undo." });
  rejectBtn.onclick = async (e) => {
    e.stopPropagation();
    const cut = !(node.classList.contains("rejected")
                  || node.classList.contains("bystander"));
    setCursor(node, { scroll: false });
    await cutCard(node, cut);
    rejectBtn.textContent = cutLabel(item, node);
  };

  node.append(frame, el("div", { className: "body" }, facts),
              el("div", { className: "verdict-row" }, starRow, rejectBtn));
  return node;
}

async function reject(id, rejected) {
  await api(`/api/detections/${id}`, {
    method: "POST", body: JSON.stringify({ rejected, reviewed: true }) });
}

/* ── culling by hand ──────────────────────────────────────────
   The assisted cull measures sharpness and framing and is right most of the
   time. This is for the rest of it, and for the judgements no measurement
   makes — the one where the light is doing something, the one where the
   driver is looking at you. Going through a shoot at one card a second
   needs hands on the keyboard, not a mouse trip to a small ✕ each time. */

const KEYS = [
  ["J  /  →", "next vehicle"],
  ["K  /  ←", "previous"],
  ["X  /  Del", "reject — instantly, no confirming"],
  ["U", "put a rejected one back"],
  ["1 – 5", "give it that many stars"],
  ["0", "clear the stars, back to the measured rating"],
  ["Enter", "keep it and move on"],
  ["?", "this list"],
];

function cards() { return $$("#grid .card"); }

function setCursor(node, { scroll = true } = {}) {
  $$("#grid .card.current").forEach((c) => c.classList.remove("current"));
  if (!node) { state.cursor = null; return; }
  node.classList.add("current");
  state.cursor = node;
  if (scroll) node.scrollIntoView({ block: "center", behavior: "smooth" });
}

function moveCursor(step) {
  const all = cards();
  if (!all.length) return;
  const at = state.cursor ? all.indexOf(state.cursor) : -1;
  const next = at < 0 ? (step > 0 ? 0 : all.length - 1)
                      : Math.min(all.length - 1, Math.max(0, at + step));
  setCursor(all[next]);
}

async function setStars(node, stars) {
  const id = Number(node.dataset.id);
  if (!id) return;
  const saved = await api(`/api/detections/${id}`, {
    method: "POST", body: JSON.stringify({ stars, reviewed: true }) });
  node.dataset.stars = saved.stars || "";
  const pill = node.querySelector(".stars");
  if (pill) {
    pill.textContent = saved.stars ? "★".repeat(saved.stars) : "—";
    pill.classList.toggle("by-hand", !!saved.by_hand);
    pill.title = saved.by_hand
      ? `${saved.stars} stars, given by you — this is what Write XMP will use`
      : "Rated by the cull";
  }
  toast(saved.by_hand ? `${saved.stars} stars`
                      : "Back to the cull's rating");
  if (state.dialogNode === node) renderFrameControls(node);
}

/* Reject means "get this out of my way", and what that should do depends
   on what else is in the photograph.

   A frame holding several vehicles is usually a good frame of one car with
   others behind it, so rejecting there means "this is not the car" -- the
   detection leaves the vehicle and stops speaking for the frame, and the
   frame itself is untouched. Only when the vehicle is the sole thing
   detected in the frame does rejecting mean rejecting the photograph.

   One button either way: the distinction is real but it is not one anyone
   should have to make by picking the right control. */
async function cutCard(node, cut) {
  const id = Number(node.dataset.id);
  if (!id) return;
  const alone = ((node._item || {}).in_frame || 1) <= 1;
  try {
    await api(`/api/detections/${id}`, {
      method: "POST",
      body: JSON.stringify(alone ? { rejected: cut, reviewed: true }
                                 : { bystander: cut, reviewed: true }),
    });
  } catch (err) { toast(err.message); return; }

  if (node._item) node._item[alone ? "rejected" : "bystander"] = cut;
  node.classList.toggle("rejected", cut && alone);
  node.classList.toggle("bystander", cut && !alone);
  if (cut && !alone) {
    toast("Taken out of this vehicle — the frame is untouched");
  }
  loadSummary();
  if (state.dialogNode === node) renderFrame();
}

// What the button should say, given what rejecting would actually do here.
function cutLabel(item, node) {
  const alone = ((item || {}).in_frame || 1) <= 1;
  const cut = node.classList.contains("rejected") || node.classList.contains("bystander");
  if (cut) return alone ? "Restore" : "Put back";
  return alone ? "Reject" : "Not this car";
}

// The full-frame dialog is where a hard call actually gets made -- soft or
// sharp, kept or not -- so it carries the same reject and star controls as
// the card behind it rather than making someone close it first. Both draw
// off the same card node, through the same setStars/cutCard, so the two
// views can never disagree about a vehicle's state.
/* The full-frame view: the photograph, everything read off it down the
   side, and a way through the rest of the vehicle's frames without going
   back to the grid between each one. It used to be the picture, its path
   and a Close button -- so culling a twenty-five frame burst meant open,
   judge, close, click the next, twenty-five times. */
function openFrame(node) {
  if (!node) return;
  state.dialogNode = node;
  setCursor(node, { scroll: false });
  renderFrame();
  const dialog = $("#frame-dialog");
  if (!dialog.open) dialog.showModal();
}

// Which frames the arrows walk: the ones on screen, in the order shown.
function frameSiblings() {
  const all = cards();
  return { all, at: all.indexOf(state.dialogNode) };
}

function stepFrame(by) {
  const { all, at } = frameSiblings();
  if (at < 0 || !all.length) return;
  const next = all[Math.min(all.length - 1, Math.max(0, at + by))];
  if (next && next !== state.dialogNode) openFrame(next);
}

function renderFrame() {
  const node = state.dialogNode;
  if (!node) return;
  const item = node._item || {};
  const attrs = item.attributes || {};

  $("#frame-img").src = item.frame_url || "";
  $("#frame-title").textContent = item.title || item.cls || "Vehicle";
  $("#frame-caption").textContent = item.image_path || "";

  // Everything known about this one frame, as labelled lines.
  const out = $("#frame-readout");
  const line = (label, value, cls) => {
    if (value === null || value === undefined || value === "") return;
    out.append(el("div", { className: "read" },
      el("span", { className: "lbl", textContent: label }),
      el("span", { className: "val" + (cls ? " " + cls : ""), textContent: String(value) })));
  };
  out.replaceChildren();
  line("Plate", item.plate || "—");
  line("No.", item.number || "—");
  if (item.number && item.number_conf != null) {
    line("Read", `${Math.round(item.number_conf * 100)}% ${item.number_source || ""}`.trim());
  }
  const saveAttrs = (patch) => api(`/api/detections/${item.id}`, {
    method: "POST", body: JSON.stringify({ attributes: patch, reviewed: true }),
  }).then(() => loadSummary()).catch((err) => toast(err.message));

  const chipRow = (label, values, opts) => {
    const row = el("div", { className: "read" },
      el("span", { className: "lbl", textContent: label }));
    row.append(chipField({ values, ...opts }));
    out.append(row);
  };
  chipRow("Team", attrs.team ? [attrs.team] : [], {
    single: true, placeholder: "team or entrant",
    onChange: (list) => { attrs.team = list[0] || null; saveAttrs({ team: list[0] || "" }); },
  });
  if (!attrs.team && identified(attrs)) {
    out.lastChild.append(el("span", { className: "val muted-note",
                                      textContent: "Independent" }));
  }
  const sponsors = (attrs.sponsors || []).filter((s) => s && s !== attrs.team);
  chipRow("Livery", sponsors, {
    placeholder: "sponsor",
    onChange: (list) => { attrs.sponsors = list; saveAttrs({ sponsors: list }); },
  });
  if (item.who) line("Driver", item.who);
  if (attrs.body_type) line("Body", attrs.body_type);
  if (item.camera) line("Camera", shortCamera(item.camera));
  if (item.sharpness != null) {
    const verdict = item.rating_verdict || item.sharpness_verdict || "";
    line("Sharpness", `${item.sharpness.toFixed(2)}${verdict ? " · " + verdict : ""}`);
  }
  if (item.cull_reason) line("Culled", item.cull_reason, "unverified");

  const stars = Number(node.dataset.stars) || 0;
  const box = $("#frame-stars");
  box.replaceChildren(...[1, 2, 3, 4, 5].map((n) => {
    const b = el("button", { className: "star" + (n <= stars ? " on" : ""),
                             textContent: "★",
                             title: `${n} star${n === 1 ? "" : "s"}` });
    b.onclick = () => setStars(node, n);
    return b;
  }));
  $("#frame-clear").onclick = () => setStars(node, 0);

  const cut = node.classList.contains("rejected")
              || node.classList.contains("bystander");
  const rejectBtn = $("#frame-reject");
  rejectBtn.textContent = cutLabel(item, node);
  rejectBtn.title = (item.in_frame || 1) > 1
    ? "Several vehicles in this frame. This takes this one out of the "
      + "vehicle; the photograph itself is untouched."
    : "Reject this vehicle. Nothing is written to the photograph either way.";
  rejectBtn.onclick = () => cutCard(node, !cut);

  /* A frame holding several vehicles used to get its own "Not the main car"
     button here. Reject was rewired to do that job instead -- on a frame
     with other cars in it, Reject takes this car out and leaves the frame
     alone -- so the button went, and this is what is left of it.

     The button went from the page and this line did not, which threw on
     every attempt to open a frame and took the whole full-frame view with
     it: clicking a picture to see it larger simply stopped working. */

  const { all, at } = frameSiblings();
  $("#frame-pos").textContent = all.length > 1 ? `${at + 1} of ${all.length}` : "";
  $("#frame-prev").disabled = at <= 0;
  $("#frame-next").disabled = at < 0 || at >= all.length - 1;
}

// Kept as the name the rest of the app calls after a star or a reject.
function renderFrameControls(node) {
  if (state.dialogNode === node) renderFrame();
}

$("#frame-prev").onclick = () => stepFrame(-1);
$("#frame-next").onclick = () => stepFrame(1);

document.addEventListener("keydown", async (e) => {
  if (state.screen !== "review") return;
  // Never steal a key from someone typing a competition number.
  const tag = (e.target.tagName || "").toLowerCase();
  if (tag === "input" || tag === "select" || tag === "textarea") return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;

  // The dialog shows one frame at a time and traps focus on it, so j/k
  // moving a cursor the person cannot see would only end up culling
  // whichever card it landed on next, not the one still on screen.
  const dialogOpen = $("#frame-dialog").open;
  const key = e.key;
  if (key === "?") { e.preventDefault(); showKeys(); return; }
  // Escape leaves the vehicle you are in, the way the back arrow does.
  // The dialog handles its own Escape natively, so it goes first.
  if (key === "Escape" && !dialogOpen && state.stack) {
    e.preventDefault(); closeStack(); return;
  }
  // In the full-frame view the arrows walk the vehicle's frames, which is
  // the whole point of being in it: judge, move on, without closing.
  if (dialogOpen && (key === "ArrowRight" || key === "j")) {
    e.preventDefault(); stepFrame(1); return;
  }
  if (dialogOpen && (key === "ArrowLeft" || key === "k")) {
    e.preventDefault(); stepFrame(-1); return;
  }
  if (!dialogOpen && (key === "j" || key === "ArrowRight" || key === "ArrowDown")) {
    e.preventDefault(); moveCursor(1); return;
  }
  if (!dialogOpen && (key === "k" || key === "ArrowLeft" || key === "ArrowUp")) {
    e.preventDefault(); moveCursor(-1); return;
  }

  const node = dialogOpen ? state.dialogNode : (state.cursor || cards()[0]);
  if (!node) return;
  if (!dialogOpen && !state.cursor) { setCursor(node); return; }

  if (key === "x" || key === "X" || key === "Delete" || key === "Backspace") {
    e.preventDefault();
    await cutCard(node, true);
    if (!dialogOpen) moveCursor(1);
  } else if (key === "u" || key === "U") {
    e.preventDefault();
    await cutCard(node, false);
  } else if (key === "Enter") {
    e.preventDefault();
    await api(`/api/detections/${node.dataset.id}`, {
      method: "POST", body: JSON.stringify({ reviewed: true }) });
    node.classList.add("saved");
    if (!dialogOpen) moveCursor(1);
  } else if (key >= "0" && key <= "5") {
    e.preventDefault();
    await setStars(node, Number(key));
  }
});

function showKeys() {
  toast(KEYS.map(([k, what]) => `${k} — ${what}`).join("\n"), 7000);
}

$("#btn-keys").onclick = showKeys;
$("#sort").onchange = () => {
  state.sort = $("#sort").value;
  state.offset = 0;
  state.stack = null;
  setCursor(null);
  loadGrid();
};

// "3 stars and up" as one click, the way Aftershoot's toolbar does it,
// rather than only being able to sort by rating and scroll to where it
// stops being good enough. Clicking the star already active clears the
// filter -- there is no separate "off" button to hunt for.
renderStarFilter();
setFacet("number");
function renderStarFilter() {
  const box = $("#star-filter");
  const clear = el("button", { className: "ghost star-filter-clear",
    textContent: "Any", title: "Show every rating" });
  clear.classList.toggle("on", !state.minStars);
  clear.onclick = () => setMinStars(null);
  box.replaceChildren(clear, ...[1, 2, 3, 4, 5].map((n) => {
    const b = el("button", {
      className: "star" + (state.minStars && n <= state.minStars ? " on" : ""),
      textContent: "★", title: `${n}${n === 1 ? " star" : " stars"} and up`,
    });
    b.onclick = () => setMinStars(state.minStars === n ? null : n);
    return b;
  }));
}
function setMinStars(n) {
  state.minStars = n;
  state.offset = 0;
  state.stack = null;
  setCursor(null);
  renderStarFilter();
  loadGrid();
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

/* Three tabs, not five. "By number" and "Plates" were never really views
   -- both did nothing until you picked something from a sidebar they
   turned on -- so they sat oddly beside "All" and "Rejected", which are
   states a vehicle is in. The sidebar is always there now and does the
   picking; these three say which vehicles it is picking from.

   "Needs review" also used to be both a tab and the default sort, saying
   the same thing twice in two controls a hand's width apart. */
function setView(view) {
  state.view = view;
  state.offset = 0;
  state.stack = null;
  state.facetPick = null;      // a tab is a fresh start, not a narrowing
  $$("#view-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  renderFacets();
  loadGrid(false);
  loadSummary();
}

$$("#view-tabs button").forEach((b) => { b.onclick = () => setView(b.dataset.view); });

/* The sidebar. Numbers or plates, whichever is being browsed, with the one
   in force shown as a filter that can be cleared. */
function setFacet(kind) {
  state.facet = kind;
  $$("#facet-tabs button").forEach((b) =>
    b.classList.toggle("active", b.dataset.facet === kind));
  $("#number-list").hidden = kind !== "number";
  $("#plate-list").hidden = kind !== "plate";
  renderFacets();
}
$$("#facet-tabs button").forEach((b) => { b.onclick = () => setFacet(b.dataset.facet); });

function pickFacet(kind, value) {
  state.facetPick = { kind, value };
  state.offset = 0;
  state.stack = null;
  renderFacets();
  loadGrid(false);
}

$("#facet-clear").onclick = () => {
  state.facetPick = null;
  state.offset = 0;
  state.stack = null;
  renderFacets();
  loadGrid(false);
};

function renderFacets() {
  const pick = state.facetPick;
  $("#facet-clear").hidden = !pick;
  if (pick) {
    $("#facet-clear").textContent =
      `Clear ${pick.kind === "plate" ? pick.value : "#" + pick.value}`;
  }
  $$("#number-list li").forEach((li) => li.classList.toggle(
    "active", Boolean(pick) && pick.kind === "number" && li.dataset.value === pick.value));
  $$("#plate-list li").forEach((li) => li.classList.toggle(
    "active", Boolean(pick) && pick.kind === "plate" && li.dataset.value === pick.value));
}

$("#job-select").onchange = (e) => { state.jobId = Number(e.target.value); refreshReview(); };
/* Infinite scroll. A button meant that going through a shoot of two
   thousand frames was scroll, reach for the mouse, click "Load more",
   scroll again, over and over.

   Driven by the scroll event rather than an IntersectionObserver: the
   observer is the tidier mechanism but it did not fire at all in one of
   the surfaces this gets looked at in, and a feed that silently stops
   feeding is worse than a plain listener.

   A page of detections can also collapse into very few stacks -- a hundred
   and twenty frames of one car is one tile -- so a load that leaves the
   pane unscrollable pulls the next page too, up to a few in a row, rather
   than stranding someone at a short page with no way to ask for more. */
function maybeLoadMore() {
  const pane = $("#grid-wrap");
  if (!pane || state.loadingMore) return;
  if (state.screen !== "review") return;
  if (state.items.length >= state.total) return;
  // Only hold off while there is still page left to scroll through; once
  // the bottom is in reach, fetch the next one.
  const room = pane.scrollHeight - pane.clientHeight;
  if (room > 0 && pane.scrollTop < room - 600) return;
  state.loadingMore = true;
  state.offset += state.limit;
  loadGrid(true).finally(() => {
    state.loadingMore = false;
    maybeLoadMore();
  });
}

function renderFoot() {
  const foot = $("#more");
  const seen = state.items.length;
  foot.hidden = seen >= state.total;
  foot.textContent =
    `${seen.toLocaleString()} of ${state.total.toLocaleString()}…`;
}

$("#grid-wrap").addEventListener("scroll", () => maybeLoadMore(), { passive: true });

let searchTimer;
$("#search").oninput = (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.search = e.target.value.trim();
    state.offset = 0;
    state.stack = null;
    loadGrid(false);
  }, 250);
};

$("#frame-close").onclick = () => $("#frame-dialog").close();
$("#frame-dialog").onclick = (e) => { if (e.target.id === "frame-dialog") $("#frame-dialog").close(); };
// Covers every way the dialog can close -- the button, the backdrop, Escape
// -- in one place, so a stale node can never linger as the keyboard's target.
$("#frame-dialog").addEventListener("close", () => { state.dialogNode = null; });

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

/* Grouping again after the fact. It works from the stored crops, so it costs
   no photo reads -- which makes it the way an album already scanned picks up
   an improvement to grouping without a rescan. The endpoint existed from the
   start and nothing in the interface ever called it. */
// Sorts the album into one pile per car. Renamed from "Regroup", which said
// what the code did rather than what the photographer wanted, and which was
// routinely read as "rescan".
$("#btn-group").onclick = async () => {
  const button = $("#btn-group");
  const was = button.textContent;
  button.disabled = true;
  button.textContent = "Grouping…";
  try {
    const out = await api(`/api/jobs/${state.jobId}/group`, { method: "POST" });
    // Redraw first, then report. Toasting before the refresh meant a failure
    // anywhere in redrawing the grid replaced the result with its own error,
    // so a regroup that worked looked like one that had not.
    try { await refreshReview(); } catch {}
    toast(out.looked
      ? `${out.vehicles} vehicles in ${out.groups} piles (looked at ${out.looked} new crops)`
      : `${out.vehicles} vehicles in ${out.groups} piles`);
  } catch (err) {
    toast(err.message);
  } finally {
    button.disabled = false;
    button.textContent = was;
  }
};

// The other half of the rename: something that actually rescans.
$("#btn-rescan").onclick = async () => {
  const album = state.album || {};
  if (!confirm(
      "Read the photographs again and redo this album?\n\n"
      + "Everything found before is thrown away and the scan starts over. "
      + "Star ratings you gave by hand are kept.\n\n"
      + "Your photographs are not touched.")) return;
  try {
    await api("/api/reset/detections?job_id=" + state.jobId, { method: "POST" });
    await runStage({ id: state.jobId, root: album.root, label: album.label },
                   "all");
  } catch (err) { toast(err.message); }
};

$("#btn-dry").onclick = async () => {
  const r = await api(`/api/jobs/${state.jobId}/write`, {
    method: "POST", body: JSON.stringify({ dry_run: true }) });
  toast(`${r.frames} frames would be keyworded`);
};

$("#btn-write").onclick = async () => {
  // Say everything that is about to be written. It is no longer only
  // keywords: the cull's verdict goes out as a star rating and a colour
  // label, and a dialog that does not mention them is asking for consent to
  // something else.
  if (!confirm(
    "Write to XMP sidecars (RAW) and into the files themselves (JPEG)?\n\n"
    + "  • keywords and caption\n"
    + "  • a star rating and a colour label from the cull\n\n"
    + "A rating or label you have already set is never overwritten.")) return;
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
    // beforeunload cannot wait on a request, so this has to be known before
    // the person ever reaches for the close button.
    try {
      state.closeToTray = (await api("/api/settings")).settings.close_to_tray;
    } catch { state.closeToTray = false; }

    $("#splash").classList.add("gone");
    setTimeout(() => { $("#splash").hidden = true; }, 500);
    $("#app").hidden = false;
    pollHealth();

    if (scan.active) {
      show("scan");
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
