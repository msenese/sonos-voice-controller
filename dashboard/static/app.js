const SERIES_COLORS = {
  "noise": "var(--series-1)",
  "sonos pause": "var(--series-2)",
  "sonos play": "var(--series-3)",
  "unknown": "var(--series-4)",
};
const LABEL_ORDER = ["noise", "sonos pause", "sonos play", "unknown"];

const metersEl = document.getElementById("meters");
const meterEls = {};
for (const label of LABEL_ORDER) {
  const row = document.createElement("div");
  row.className = "meter-row";
  row.innerHTML = `
    <div class="meter-label">${label}</div>
    <div class="meter-track"><div class="meter-fill" style="background:${SERIES_COLORS[label]}"></div></div>
    <div class="meter-value">0%</div>
  `;
  metersEl.appendChild(row);
  meterEls[label] = {
    fill: row.querySelector(".meter-fill"),
    value: row.querySelector(".meter-value"),
  };
}

let configInitialized = false;
const sliders = {
  threshold: document.getElementById("threshold"),
  sonos_play_threshold: document.getElementById("sonos_play_threshold"),
  cooldown: document.getElementById("cooldown"),
  consecutive_required: document.getElementById("consecutive_required"),
};

function fmt(key, v) {
  if (key === "cooldown") return `${v}s`;
  if (key === "consecutive_required") return `${v}`;
  return Number(v).toFixed(2);
}

for (const [key, el] of Object.entries(sliders)) {
  el.addEventListener("change", async () => {
    document.getElementById(`${key}-value`).textContent = fmt(key, el.value);
    const body = {};
    body[key] = el.value;
    await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  });
  el.addEventListener("input", () => {
    document.getElementById(`${key}-value`).textContent = fmt(key, el.value);
  });
}

function setBadge(el, textEl, status, text) {
  el.classList.remove("good", "warning", "critical");
  if (status) el.classList.add(status);
  textEl.textContent = text;
}

function timeAgo(ts) {
  if (!ts) return "";
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}

async function pollState() {
  let state;
  try {
    const res = await fetch("/api/state");
    state = await res.json();
  } catch (e) {
    setBadge(document.getElementById("connection-badge"), document.getElementById("connection-text"), "critical", "dashboard offline");
    return;
  }

  const scores = state.scores || {};
  for (const label of LABEL_ORDER) {
    const score = scores[label] || 0;
    const pct = Math.round(score * 100);
    meterEls[label].fill.style.width = `${pct}%`;
    meterEls[label].value.textContent = `${pct}%`;
  }

  const connBadge = document.getElementById("connection-badge");
  const connText = document.getElementById("connection-text");
  const statusMap = {
    connected: ["good", "connected"],
    connecting: ["warning", "connecting"],
    disconnected: ["critical", "disconnected"],
    unknown: [null, "unknown"],
  };
  const [status, text] = statusMap[state.connection_status] || statusMap.unknown;
  setBadge(connBadge, connText, status, text);

  const muteBadge = document.getElementById("mute-badge");
  const muteText = document.getElementById("mute-text");
  if (state.muted === true) {
    setBadge(muteBadge, muteText, "warning", "muted");
  } else if (state.muted === false) {
    setBadge(muteBadge, muteText, "good", "unmuted");
  } else {
    setBadge(muteBadge, muteText, null, "mute: unknown");
  }

  const history = state.history || [];
  const lastDetectedEl = document.getElementById("last-detected");
  if (history.length) {
    const last = history[history.length - 1];
    lastDetectedEl.classList.remove("empty");
    lastDetectedEl.innerHTML = `<strong>${last.label}</strong> &middot; ${(last.score * 100).toFixed(0)}% &middot; ${timeAgo(last.timestamp)}`;
  }

  const historyListEl = document.getElementById("history-list");
  if (history.length) {
    historyListEl.innerHTML = history
      .slice()
      .reverse()
      .map(h => `<li><span>${h.label} (${(h.score * 100).toFixed(0)}%)</span><span class="ts">${timeAgo(h.timestamp)}</span></li>`)
      .join("");
  }

  if (!configInitialized && state.config) {
    for (const [key, el] of Object.entries(sliders)) {
      el.value = state.config[key];
      document.getElementById(`${key}-value`).textContent = fmt(key, el.value);
    }
    configInitialized = true;
  }
}

async function pollSystem() {
  let stat;
  try {
    const res = await fetch("/api/system");
    stat = await res.json();
  } catch (e) {
    return;
  }

  if (stat.uptime_seconds != null) {
    const hours = Math.floor(stat.uptime_seconds / 3600);
    const mins = Math.floor((stat.uptime_seconds % 3600) / 60);
    document.getElementById("stat-uptime").textContent = `${hours}h ${mins}m`;
  }
  if (stat.cpu_temp_c != null) {
    document.getElementById("stat-temp").textContent = `${stat.cpu_temp_c.toFixed(1)}°C`;
  }
  if (stat.mem_used_kb != null && stat.mem_total_kb != null) {
    const usedMb = Math.round(stat.mem_used_kb / 1024);
    const totalMb = Math.round(stat.mem_total_kb / 1024);
    document.getElementById("stat-mem").textContent = `${usedMb}/${totalMb} MB`;
  }
}

const micSlider = document.getElementById("mic-level");
micSlider.addEventListener("input", () => {
  document.getElementById("mic-level-value").textContent = micSlider.value;
});
micSlider.addEventListener("change", async () => {
  await fetch("/api/mic-level", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ level: Number(micSlider.value) }),
  });
});

let lastRecordedFilename = null;
const trainStatus = document.getElementById("train-status");
const uploadBtn = document.getElementById("train-upload");

document.getElementById("train-record").addEventListener("click", async () => {
  const label = document.getElementById("train-label").value;
  const duration = Number(document.getElementById("train-duration").value);
  trainStatus.textContent = `Recording ${duration}s of "${label}"...`;
  uploadBtn.disabled = true;
  try {
    const res = await fetch("/api/train/record", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label, duration }),
    });
    const data = await res.json();
    if (!res.ok) {
      trainStatus.textContent = `Error: ${data.error}`;
      return;
    }
    lastRecordedFilename = data.filename;
    trainStatus.textContent = `Recorded ${data.filename}. Ready to upload.`;
    uploadBtn.disabled = false;
  } catch (e) {
    trainStatus.textContent = `Error: ${e}`;
  }
});

uploadBtn.addEventListener("click", async () => {
  if (!lastRecordedFilename) return;
  trainStatus.textContent = `Uploading ${lastRecordedFilename}...`;
  uploadBtn.disabled = true;
  try {
    const res = await fetch("/api/train/upload", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename: lastRecordedFilename }),
    });
    const data = await res.json();
    if (!res.ok) {
      trainStatus.textContent = `Error: ${data.error}`;
      uploadBtn.disabled = false;
      return;
    }
    trainStatus.textContent = `Uploaded ${data.uploaded} as "${data.label}".`;
    lastRecordedFilename = null;
    loadSampleCounts();
  } catch (e) {
    trainStatus.textContent = `Error: ${e}`;
    uploadBtn.disabled = false;
  }
});

async function loadSampleCounts() {
  const statusEl = document.getElementById("sample-counts-status");
  try {
    const res = await fetch("/api/model/sample-counts");
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    LABEL_ORDER.forEach((label, i) => {
      const info = data.counts[label];
      const el = document.getElementById(`sample-count-${i}`);
      if (!info) { el.textContent = "—"; return; }
      const sign = info.new > 0 ? "+" : "";
      el.textContent = info.new !== 0 ? `${info.total} (${sign}${info.new} new)` : `${info.total}`;
    });
    statusEl.textContent = data.baseline_at
      ? `"New" counts are since the last retrain.`
      : `Retrain once to start tracking "new since last retrain".`;
  } catch (e) {
    statusEl.textContent = `Could not load sample counts: ${e.message}`;
  }
}

const modelStatus = document.getElementById("model-status");
const retrainBtn = document.getElementById("model-retrain");
const buildBtn = document.getElementById("model-build");
const activateBtn = document.getElementById("model-activate");

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function pollJob(jobId) {
  while (true) {
    await sleep(2000);
    const res = await fetch(`/api/model/job/${jobId}/status`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "job status check failed");
    if (data.finished) return data.finishedSuccessful;
  }
}

function renderConfusionMatrix(classNames, matrix) {
  const flat = matrix.flat();
  const max = Math.max(...flat, 1);
  const table = document.getElementById("confusion-matrix");
  let html = "<tr><th></th>" + classNames.map(c => `<th style="padding:4px 8px;font-weight:600;">${c}</th>`).join("") + "</tr>";
  matrix.forEach((row, i) => {
    html += `<tr><th style="padding:4px 8px;text-align:right;font-weight:600;">${classNames[i] || ""}</th>`;
    row.forEach(value => {
      const alpha = 0.12 + 0.8 * (value / max);
      const color = alpha > 0.55 ? "#fff" : "var(--text-primary)";
      html += `<td style="padding:6px 10px;text-align:center;background:rgba(42,120,214,${alpha});color:${color};">${value}</td>`;
    });
    html += "</tr>";
  });
  table.innerHTML = html;
}

async function loadModelMetrics() {
  const res = await fetch("/api/model/metrics");
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "failed to load metrics");
  const metrics = (data.modelValidationMetrics || [])[0];
  if (!metrics) throw new Error("no validation metrics available");
  document.getElementById("model-accuracy").textContent = `${(metrics.accuracy * 100).toFixed(1)}%`;
  document.getElementById("model-loss").textContent = metrics.loss.toFixed(3);
  renderConfusionMatrix(data.classNames, metrics.confusionMatrix);
  document.getElementById("model-metrics").style.display = "block";
}

retrainBtn.addEventListener("click", async () => {
  retrainBtn.disabled = true;
  buildBtn.disabled = true;
  activateBtn.disabled = true;
  document.getElementById("model-metrics").style.display = "none";
  modelStatus.textContent = "Starting retrain job...";
  try {
    const startRes = await fetch("/api/model/retrain/start", { method: "POST" });
    const startData = await startRes.json();
    if (!startRes.ok) throw new Error(startData.error);
    modelStatus.textContent = "Retraining on current dataset (this can take a few minutes)...";
    const success = await pollJob(startData.job_id);
    if (!success) throw new Error("retrain job did not finish successfully");
    modelStatus.textContent = "Retrain complete. Loading accuracy...";
    await loadModelMetrics();
    await fetch("/api/model/sample-counts/snapshot", { method: "POST" });
    await loadSampleCounts();
    modelStatus.textContent = "Retrain complete. Review the results, then build for the Pi.";
    buildBtn.disabled = false;
  } catch (e) {
    modelStatus.textContent = `Error: ${e.message}`;
  } finally {
    retrainBtn.disabled = false;
  }
});

buildBtn.addEventListener("click", async () => {
  buildBtn.disabled = true;
  activateBtn.disabled = true;
  modelStatus.textContent = "Starting build job...";
  try {
    const startRes = await fetch("/api/model/build/start", { method: "POST" });
    const startData = await startRes.json();
    if (!startRes.ok) throw new Error(startData.error);
    modelStatus.textContent = "Building deployment for Linux (AARCH64)...";
    const success = await pollJob(startData.job_id);
    if (!success) throw new Error("build job did not finish successfully");
    modelStatus.textContent = "Build complete. Downloading to the Pi...";
    const dlRes = await fetch("/api/model/download", { method: "POST" });
    const dlData = await dlRes.json();
    if (!dlRes.ok) throw new Error(dlData.error);
    modelStatus.textContent = `Downloaded new model (${(dlData.size / 1024).toFixed(0)} KB). Ready to activate.`;
    activateBtn.disabled = false;
  } catch (e) {
    modelStatus.textContent = `Error: ${e.message}`;
    buildBtn.disabled = false;
  }
});

activateBtn.addEventListener("click", async () => {
  const ok = confirm("This will replace the live model and restart the keyword-spotting service for a few seconds. Continue?");
  if (!ok) return;
  activateBtn.disabled = true;
  modelStatus.textContent = "Activating new model...";
  try {
    const res = await fetch("/api/model/activate", { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    modelStatus.textContent = "New model activated and ei-runner restarted.";
  } catch (e) {
    modelStatus.textContent = `Error: ${e.message}`;
    activateBtn.disabled = false;
  }
});

pollState();
pollSystem();
loadSampleCounts();
setInterval(pollState, 1000);
setInterval(pollSystem, 5000);
