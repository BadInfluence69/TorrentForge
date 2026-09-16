/* TorrentForge front end: browse a path, start a build job, poll it, download. */
"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  cwd: "",
  selected: "",
  job: null,
  poll: null,
  outputDir: "",
  drives: [],
};

/* ------------------------------------------------------------------ utils */
function toast(message, isError) {
  const node = document.createElement("div");
  node.className = "toast" + (isError ? " err" : "");
  node.textContent = message;
  document.body.appendChild(node);
  setTimeout(() => node.remove(), 3600);
}

function humanSize(bytes) {
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let value = Number(bytes) || 0;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) { value /= 1024; i += 1; }
  return (i === 0 ? value.toFixed(0) : value.toFixed(2)) + " " + units[i];
}

function humanTime(seconds) {
  seconds = Math.max(0, Math.round(seconds));
  if (seconds < 60) return seconds + "s";
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  if (m < 60) return m + "m " + s + "s";
  return Math.floor(m / 60) + "h " + (m % 60) + "m";
}

async function api(url, options) {
  const response = await fetch(url, options);
  let payload = {};
  try { payload = await response.json(); } catch (err) { payload = {}; }
  if (!response.ok) throw new Error(payload.error || ("HTTP " + response.status));
  return payload;
}

async function postJSON(url, body) {
  return api(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

/* --------------------------------------------------------------- browsing */
async function browse(path) {
  try {
    const hidden = $("show-hidden").checked ? "1" : "0";
    const data = await api("/api/browse?path=" + encodeURIComponent(path || "") + "&hidden=" + hidden);
    state.cwd = data.path;
    state.drives = data.drives || state.drives;
    renderDrives();
    renderCrumbs(data.crumbs);
    renderListing(data);
  } catch (err) {
    toast(err.message, true);
  }
}

function renderDrives() {
  const box = $("drives");
  box.innerHTML = "";
  state.drives.forEach((drive) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = drive.label || drive.name;
    if (state.cwd.toUpperCase().startsWith(drive.path.toUpperCase())) {
      button.classList.add("active");
    }
    button.onclick = () => browse(drive.path);
    box.appendChild(button);
  });
}

function renderCrumbs(crumbs) {
  const box = $("crumbs");
  box.innerHTML = "";
  (crumbs || []).forEach((crumb, index) => {
    if (index) {
      const sep = document.createElement("span");
      sep.className = "sep";
      sep.textContent = " \\ ";
      box.appendChild(sep);
    }
    const link = document.createElement("a");
    link.textContent = crumb.name;
    link.onclick = () => browse(crumb.path);
    box.appendChild(link);
  });
}

function row(icon, name, size, cls, handler) {
  const li = document.createElement("li");
  li.className = cls || "";
  li.innerHTML =
    '<span class="ico">' + icon + '</span><span class="nm"></span><span class="sz"></span>';
  li.querySelector(".nm").textContent = name;
  li.querySelector(".sz").textContent = size || "";
  if (handler) li.onclick = handler;
  return li;
}

function renderListing(data) {
  const list = $("listing");
  list.innerHTML = "";

  if (data.parent) {
    list.appendChild(row("&#8617;", ".. (up one level)", "", "up", () => browse(data.parent)));
  }
  data.dirs.forEach((dir) => {
    const li = row("&#128193;", dir.name, "", "dir", () => browse(dir.path));
    li.ondblclick = () => selectPath(dir.path);
    list.appendChild(li);
  });
  data.files.forEach((file) => {
    list.appendChild(
      row("&#128196;", file.name, file.human || humanSize(file.size), "file", () =>
        selectPath(file.path)
      )
    );
  });
  if (!data.dirs.length && !data.files.length) {
    list.appendChild(row("", "this folder is empty", "", "empty", null));
  }
}

/* -------------------------------------------------------------- selection */
async function selectPath(path) {
  if (!path) return;
  state.selected = path;
  $("source-path").value = path;
  $("selected").classList.remove("hidden");
  $("sel-name").textContent = path;
  $("sel-meta").textContent = "reading size\u2026";
  $("btn-create").disabled = true;
  try {
    const hidden = $("include-hidden").checked ? "1" : "0";
    const info = await api("/api/info?path=" + encodeURIComponent(path) + "&hidden=" + hidden);
    $("sel-meta").textContent =
      (info.is_dir ? "folder" : "file") +
      " \u2022 " + info.file_count + " file" + (info.file_count === 1 ? "" : "s") +
      " \u2022 " + info.human_size +
      " \u2022 auto piece size " + info.suggested_piece_human;
    $("btn-create").disabled = info.total_size === 0;
    if (info.total_size === 0) toast("That selection has no data in it.", true);
  } catch (err) {
    $("sel-meta").textContent = err.message;
    $("btn-create").disabled = true;
  }
}

/* ------------------------------------------------------------------ build */
function collectOptions() {
  return {
    source: $("source-path").value.trim(),
    trackers: $("trackers").value,
    web_seeds: $("web-seeds").value,
    comment: $("comment").value,
    source_tag: $("source-tag").value,
    name: $("torrent-name").value,
    piece_length: parseInt($("piece-length").value, 10) || 0,
    version: $("version").value,
    private: $("private").checked,
    include_hidden: $("include-hidden").checked,
  };
}

async function createTorrent() {
  const options = collectOptions();
  if (!options.source) { toast("Pick a folder or file first.", true); return; }

  $("error-box").classList.add("hidden");
  $("step-result").classList.add("hidden");
  $("progress-wrap").classList.remove("hidden");
  $("btn-create").disabled = true;
  $("btn-cancel").classList.remove("hidden");
  setProgress(0, "starting\u2026", "");

  try {
    const data = await postJSON("/api/create", options);
    state.job = data.job;
    state.poll = setInterval(pollJob, 350);
    pollJob();
  } catch (err) {
    failBuild(err.message);
  }
}

function setProgress(percent, file, rate) {
  $("bar-fill").style.width = Math.max(0, Math.min(100, percent)) + "%";
  $("progress-percent").textContent = percent.toFixed(1) + "%";
  $("progress-file").textContent = file || "";
  $("progress-rate").textContent = rate || "";
}

async function pollJob() {
  if (!state.job) return;
  try {
    const job = await api("/api/job/" + state.job);
    if (job.state === "hashing" || job.state === "queued") {
      const rate = job.rate_human
        ? job.rate_human + (job.eta ? "  \u2022  ETA " + humanTime(job.eta) : "")
        : "";
      setProgress(
        job.percent,
        job.current + "   (" + job.done_human + " / " + job.total_human + ")",
        rate
      );
      return;
    }
    stopPolling();
    if (job.state === "done") {
      setProgress(100, "finished", "in " + humanTime(job.elapsed));
      showResult(job);
      loadHistory();
    } else {
      failBuild(job.error || "the build stopped unexpectedly");
    }
  } catch (err) {
    stopPolling();
    failBuild(err.message);
  }
}

function stopPolling() {
  if (state.poll) clearInterval(state.poll);
  state.poll = null;
  $("btn-cancel").classList.add("hidden");
  $("btn-create").disabled = false;
}

function failBuild(message) {
  stopPolling();
  $("progress-wrap").classList.add("hidden");
  const box = $("error-box");
  box.textContent = message;
  box.classList.remove("hidden");
}

function cell(key, value) {
  const div = document.createElement("div");
  div.className = "rcell";
  div.innerHTML = '<div class="k"></div><div class="v"></div>';
  div.querySelector(".k").textContent = key;
  div.querySelector(".v").textContent = value;
  return div;
}

function showResult(job) {
  const result = job.result || {};
  $("step-result").classList.remove("hidden");
  $("result-file").textContent = "saved as " + job.filename;

  const grid = $("result-grid");
  grid.innerHTML = "";
  grid.appendChild(cell("name", result.name));
  grid.appendChild(cell("format", result.version === "hybrid" ? "hybrid (v1 + v2)" : result.version));
  grid.appendChild(cell("total size", humanSize(result.total_size)));
  grid.appendChild(cell("files", result.file_count));
  grid.appendChild(cell("piece size", humanSize(result.piece_length)));
  grid.appendChild(cell("pieces", result.piece_count));
  if (result.infohash_v1) grid.appendChild(cell("info hash v1", result.infohash_v1));
  if (result.infohash_v2) grid.appendChild(cell("info hash v2", result.infohash_v2));
  grid.appendChild(cell("torrent file size", humanSize(result.torrent_size)));
  grid.appendChild(cell("hashing time", humanTime(result.elapsed)));

  $("magnet").value = result.magnet || "";
  const link = $("btn-download");
  link.href = "/api/download/" + job.id;
  link.setAttribute("download", job.filename);

  $("file-count").textContent = (result.files || []).length;
  const list = $("result-files");
  list.innerHTML = "";
  (result.files || []).forEach((file) => {
    const li = document.createElement("li");
    li.textContent = file.path + " ";
    const span = document.createElement("span");
    span.textContent = "(" + humanSize(file.size) + ")";
    li.appendChild(span);
    list.appendChild(li);
  });

  $("step-result").scrollIntoView({ behavior: "smooth", block: "start" });
}

/* ---------------------------------------------------------------- history */
async function loadHistory() {
  try {
    const data = await api("/api/history");
    state.outputDir = data.output_dir;
    $("output-dir").textContent = data.output_dir;
    const list = $("history");
    list.innerHTML = "";
    if (!data.items.length) {
      const li = document.createElement("li");
      li.className = "empty";
      li.textContent = "No torrents built yet.";
      list.appendChild(li);
      return;
    }
    data.items.forEach((item) => {
      const li = document.createElement("li");

      const name = document.createElement("span");
      name.className = "hname";
      name.textContent = item.name;

      const meta = document.createElement("span");
      meta.className = "hmeta";
      meta.textContent = item.human + " \u2022 " + item.modified;

      const dl = document.createElement("a");
      dl.className = "dl";
      dl.href = "/api/history/" + encodeURIComponent(item.name);
      dl.setAttribute("download", item.name);
      dl.textContent = "download";

      const del = document.createElement("button");
      del.className = "del";
      del.type = "button";
      del.textContent = "delete";
      del.onclick = async () => {
        if (!confirm("Delete " + item.name + " from the output folder?")) return;
        try {
          await api("/api/history/" + encodeURIComponent(item.name), { method: "DELETE" });
          loadHistory();
        } catch (err) { toast(err.message, true); }
      };

      li.append(name, meta, dl, del);
      list.appendChild(li);
    });
  } catch (err) {
    toast(err.message, true);
  }
}

/* ------------------------------------------------------------------- boot */
async function boot() {
  try {
    const data = await api("/api/bootstrap");
    state.drives = data.drives || [];
    state.outputDir = data.output_dir;
    $("output-dir").textContent = data.output_dir;

    const settings = data.settings || {};
    $("trackers").value = settings.trackers || "";
    $("web-seeds").value = settings.web_seeds || "";
    $("comment").value = settings.comment || "";
    $("source-tag").value = settings.source || "";
    $("private").checked = !!settings.private;
    $("include-hidden").checked = !!settings.include_hidden;
    if (settings.version) $("version").value = settings.version;
    if (settings.piece_length) $("piece-length").value = String(settings.piece_length);

    if (!data.native_picker) {
      $("btn-pick-folder").disabled = true;
      $("btn-pick-file").disabled = true;
    }
    await browse(data.start);
  } catch (err) {
    toast("Could not talk to the server: " + err.message, true);
  }
  loadHistory();
}

async function nativePick(mode) {
  const button = mode === "file" ? $("btn-pick-file") : $("btn-pick-folder");
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "waiting for dialog\u2026";
  try {
    const data = await postJSON("/api/pick", { mode: mode, start: state.cwd });
    if (data.path) {
      await selectPath(data.path);
      browse(mode === "file" ? data.path.replace(/[\\/][^\\/]*$/, "") : data.path);
    }
  } catch (err) {
    toast(err.message, true);
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("btn-toggle-browser").onclick = () => $("browser").classList.toggle("hidden");
  $("btn-pick-folder").onclick = () => nativePick("folder");
  $("btn-pick-file").onclick = () => nativePick("file");
  $("btn-use-folder").onclick = () => selectPath(state.cwd);
  $("show-hidden").onchange = () => browse(state.cwd);
  $("include-hidden").onchange = () => { if (state.selected) selectPath(state.selected); };
  $("source-path").onchange = () => selectPath($("source-path").value.trim());
  $("btn-create").onclick = createTorrent;
  $("btn-cancel").onclick = async () => {
    if (!state.job) return;
    try { await postJSON("/api/job/" + state.job + "/cancel"); } catch (err) { /* ignore */ }
  };
  $("btn-copy-magnet").onclick = async () => {
    const value = $("magnet").value;
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
      toast("Magnet link copied.");
    } catch (err) {
      $("magnet").select();
      document.execCommand("copy");
      toast("Magnet link copied.");
    }
  };
  $("btn-again").onclick = () => {
    $("step-result").classList.add("hidden");
    $("progress-wrap").classList.add("hidden");
    $("step-source").scrollIntoView({ behavior: "smooth" });
  };
  $("open-output").onclick = async () => {
    try { await postJSON("/api/open-output"); } catch (err) { toast(err.message, true); }
  };
  boot();
});
