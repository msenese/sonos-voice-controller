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
const recordBtn = document.getElementById("train-record");

recordBtn.addEventListener("click", async () => {
  const label = document.getElementById("train-label").value;
  const duration = Number(document.getElementById("train-duration").value);
  recordBtn.disabled = true;
  uploadBtn.disabled = true;
  trainStatus.textContent = "Getting ready (pausing keyword spotting)...";

  let data;
  try {
    const res = await fetch("/api/train/record/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label, duration }),
    });
    data = await res.json();
    if (!res.ok) throw new Error(data.error);
  } catch (e) {
    trainStatus.textContent = `Error: ${e.message}`;
    recordBtn.disabled = false;
    return;
  }

  // The response above only arrives once arecord has actually started, but
  // network/JS/reaction-time slack could still eat into the capture window,
  // so the recording includes a silent pre-roll before we ask you to speak.
  const preroll = data.preroll || 0;
  if (preroll > 0) {
    trainStatus.textContent = "Get ready...";
    await new Promise(resolve => setTimeout(resolve, preroll * 1000));
  }

  let remaining = duration;
  trainStatus.textContent = `🔴 Recording now — say "${label}"! ${remaining}s`;
  const tick = setInterval(() => {
    remaining -= 1;
    if (remaining > 0) {
      trainStatus.textContent = `🔴 Recording now — say "${label}"! ${remaining}s`;
    }
  }, 1000);

  await new Promise(resolve => setTimeout(resolve, duration * 1000));
  clearInterval(tick);
  trainStatus.textContent = "Finishing up (resuming keyword spotting)...";

  try {
    const res = await fetch("/api/train/record/finish", { method: "POST" });
    const finishData = await res.json();
    if (!res.ok) throw new Error(finishData.error);
    lastRecordedFilename = finishData.filename;
    trainStatus.textContent = `Recorded ${finishData.filename}. Ready to upload.`;
    uploadBtn.disabled = false;
  } catch (e) {
    trainStatus.textContent = `Error: ${e.message}`;
  } finally {
    recordBtn.disabled = false;
  }
});

uploadBtn.addEventListener("click", async () => {
  if (!lastRecordedFilename) return;
  const uploadedFilename = lastRecordedFilename;
  trainStatus.textContent = `Uploading ${uploadedFilename}...`;
  uploadBtn.disabled = true;
  try {
    const res = await fetch("/api/train/upload", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename: uploadedFilename }),
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
    openSegmentReview(uploadedFilename);
  } catch (e) {
    trainStatus.textContent = `Error: ${e}`;
    uploadBtn.disabled = false;
  }
});

// --- Sample segment review (waveform + draggable boundaries) ---

const segmentReviewEl = document.getElementById("segment-review");
const segmentStatus = document.getElementById("segment-status");
const segmentAudio = document.getElementById("segment-audio");
const segmentLengthInput = document.getElementById("segment-length");
const findSegmentsBtn = document.getElementById("find-segments-btn");
const addSegmentBtn = document.getElementById("add-segment-btn");
const splitBtn = document.getElementById("split-samples-btn");
const waveformCanvas = document.getElementById("waveform-canvas");
const waveformCtx = waveformCanvas.getContext("2d");

let audioBuffer = null;
let totalDurationMs = 0;
let segments = [];
let currentSampleId = null;
let currentFilename = null;
let draggingHandle = null;
let segmentPlaybackHandler = null;

const PLAY_ICON_Y = 18;
const PLAY_ICON_R = 9;

function cssVar(name) {
  return getComputedStyle(document.body).getPropertyValue(name).trim();
}

function msToX(ms) {
  return totalDurationMs ? (ms / totalDurationMs) * waveformCanvas.width : 0;
}

function xToMs(x) {
  return totalDurationMs ? (x / waveformCanvas.width) * totalDurationMs : 0;
}

function canvasPoint(e) {
  const rect = waveformCanvas.getBoundingClientRect();
  return {
    x: (e.clientX - rect.left) * (waveformCanvas.width / rect.width),
    y: (e.clientY - rect.top) * (waveformCanvas.height / rect.height),
  };
}

function drawWaveform() {
  const width = waveformCanvas.width;
  const height = waveformCanvas.height;
  waveformCtx.clearRect(0, 0, width, height);

  if (audioBuffer) {
    const data = audioBuffer.getChannelData(0);
    const step = Math.max(1, Math.floor(data.length / width));
    const mid = height / 2;
    waveformCtx.strokeStyle = cssVar("--series-1") || "#2a78d6";
    waveformCtx.lineWidth = 1;
    for (let x = 0; x < width; x++) {
      let min = 1.0, max = -1.0;
      const start = x * step;
      for (let i = 0; i < step; i++) {
        const v = data[start + i];
        if (v === undefined) break;
        if (v < min) min = v;
        if (v > max) max = v;
      }
      waveformCtx.beginPath();
      waveformCtx.moveTo(x + 0.5, mid + min * mid);
      waveformCtx.lineTo(x + 0.5, mid + max * mid);
      waveformCtx.stroke();
    }
  }

  segments.forEach((seg, i) => {
    const x1 = msToX(seg.startMs);
    const x2 = msToX(seg.endMs);
    waveformCtx.fillStyle = "rgba(27, 175, 122, 0.18)";
    waveformCtx.fillRect(x1, 0, x2 - x1, height);
    waveformCtx.fillStyle = "#1baf7a";
    waveformCtx.fillRect(x1 - 2, 0, 4, height);
    waveformCtx.fillRect(x2 - 2, 0, 4, height);

    // Play button for previewing just this segment's range.
    const midX = (x1 + x2) / 2;
    waveformCtx.beginPath();
    waveformCtx.arc(midX, PLAY_ICON_Y, PLAY_ICON_R, 0, Math.PI * 2);
    waveformCtx.fillStyle = "rgba(0, 0, 0, 0.55)";
    waveformCtx.fill();
    waveformCtx.beginPath();
    waveformCtx.moveTo(midX - 3, PLAY_ICON_Y - 4.5);
    waveformCtx.lineTo(midX - 3, PLAY_ICON_Y + 4.5);
    waveformCtx.lineTo(midX + 4, PLAY_ICON_Y);
    waveformCtx.closePath();
    waveformCtx.fillStyle = "#ffffff";
    waveformCtx.fill();

    waveformCtx.fillStyle = cssVar("--text-primary") || "#fff";
    waveformCtx.font = "11px sans-serif";
    waveformCtx.fillText(`#${i + 1}`, x1 + 4, height - 6);
  });
}

function playSegment(seg) {
  if (segmentPlaybackHandler) {
    segmentAudio.removeEventListener("timeupdate", segmentPlaybackHandler);
    segmentPlaybackHandler = null;
  }
  segmentAudio.currentTime = seg.startMs / 1000;
  segmentAudio.play();
  const stopAt = seg.endMs / 1000;
  segmentPlaybackHandler = () => {
    if (segmentAudio.currentTime >= stopAt) {
      segmentAudio.pause();
      segmentAudio.removeEventListener("timeupdate", segmentPlaybackHandler);
      segmentPlaybackHandler = null;
    }
  };
  segmentAudio.addEventListener("timeupdate", segmentPlaybackHandler);
}

function hitTest(point) {
  const HIT = 8;
  for (let i = 0; i < segments.length; i++) {
    const x1 = msToX(segments[i].startMs);
    const x2 = msToX(segments[i].endMs);
    const midX = (x1 + x2) / 2;
    if (Math.hypot(point.x - midX, point.y - PLAY_ICON_Y) <= PLAY_ICON_R + 2) {
      return { i, edge: "play" };
    }
  }
  for (let i = 0; i < segments.length; i++) {
    if (Math.abs(point.x - msToX(segments[i].startMs)) <= HIT) return { i, edge: "start" };
    if (Math.abs(point.x - msToX(segments[i].endMs)) <= HIT) return { i, edge: "end" };
  }
  for (let i = 0; i < segments.length; i++) {
    const x1 = msToX(segments[i].startMs);
    const x2 = msToX(segments[i].endMs);
    if (point.x > x1 + HIT && point.x < x2 - HIT) {
      return { i, edge: "move", grabOffsetMs: xToMs(point.x) - segments[i].startMs };
    }
  }
  return null;
}

waveformCanvas.addEventListener("pointerdown", (e) => {
  const hit = hitTest(canvasPoint(e));
  if (!hit) return;
  if (hit.edge === "play") {
    playSegment(segments[hit.i]);
    return;
  }
  draggingHandle = hit;
});

waveformCanvas.addEventListener("pointermove", (e) => {
  const point = canvasPoint(e);
  if (!draggingHandle) {
    const hit = hitTest(point);
    waveformCanvas.style.cursor = hit
      ? (hit.edge === "play" ? "pointer" : hit.edge === "move" ? "grab" : "ew-resize")
      : "default";
    return;
  }
  const ms = Math.max(0, Math.min(totalDurationMs, xToMs(point.x)));
  const seg = segments[draggingHandle.i];
  if (draggingHandle.edge === "start") {
    seg.startMs = Math.min(ms, seg.endMs - 50);
  } else if (draggingHandle.edge === "end") {
    seg.endMs = Math.max(ms, seg.startMs + 50);
  } else if (draggingHandle.edge === "move") {
    const width = seg.endMs - seg.startMs;
    let newStart = ms - draggingHandle.grabOffsetMs;
    newStart = Math.max(0, Math.min(totalDurationMs - width, newStart));
    seg.startMs = newStart;
    seg.endMs = newStart + width;
  }
  drawWaveform();
});

window.addEventListener("pointerup", () => { draggingHandle = null; });

waveformCanvas.addEventListener("dblclick", (e) => {
  const ms = xToMs(canvasPoint(e).x);
  const idx = segments.findIndex(s => ms >= s.startMs && ms <= s.endMs);
  if (idx >= 0) {
    segments.splice(idx, 1);
    drawWaveform();
    splitBtn.disabled = segments.length === 0;
  }
});

segmentLengthInput.addEventListener("input", () => {
  document.getElementById("segment-length-value").textContent = segmentLengthInput.value;
});

async function lookupSampleId(filename, attempts = 15, delayMs = 2000) {
  for (let i = 0; i < attempts; i++) {
    const res = await fetch(`/api/train/samples/lookup?filename=${encodeURIComponent(filename)}`);
    const data = await res.json();
    if (res.ok) return data.sample_id;
    if (i < attempts - 1) await new Promise(r => setTimeout(r, delayMs));
  }
  throw new Error("could not find uploaded sample on Edge Impulse yet — longer recordings can take a while to process, try \"Find Segments\" again in a bit");
}

async function openSegmentReview(filename) {
  segments = [];
  currentSampleId = null;
  currentFilename = filename;
  audioBuffer = null;
  totalDurationMs = 0;
  splitBtn.disabled = true;
  segmentReviewEl.style.display = "block";
  segmentAudio.src = `/training-samples/${filename}`;
  segmentStatus.textContent = "Loading waveform...";
  drawWaveform();

  try {
    const resp = await fetch(`/training-samples/${filename}`);
    const arrayBuf = await resp.arrayBuffer();
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    const audioCtx = new AudioCtx();
    audioBuffer = await audioCtx.decodeAudioData(arrayBuf);
    totalDurationMs = audioBuffer.duration * 1000;
    drawWaveform();
  } catch (e) {
    segmentStatus.textContent = `Could not load waveform: ${e.message}`;
    return;
  }

  segmentStatus.textContent = "Looking up sample on Edge Impulse (can take a bit for longer recordings)...";
  try {
    currentSampleId = await lookupSampleId(filename);
    segmentStatus.textContent = 'Ready. Click "Find Segments" or use "Add Segment" to mark boundaries manually. Click the ▶ on a segment to preview it.';
  } catch (e) {
    segmentStatus.textContent = `${e.message}`;
  }
}

findSegmentsBtn.addEventListener("click", async () => {
  if (!currentSampleId) {
    segmentStatus.textContent = "Looking up sample on Edge Impulse...";
    try {
      currentSampleId = await lookupSampleId(currentFilename, 3, 1500);
    } catch (e) {
      segmentStatus.textContent = e.message;
      return;
    }
  }
  const segmentLengthMs = Number(segmentLengthInput.value);
  segmentStatus.textContent = "Finding segments...";
  try {
    const res = await fetch(`/api/train/samples/${currentSampleId}/find-segments`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ segment_length_ms: segmentLengthMs, shift_segments: false }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    segments = data.segments;
    drawWaveform();
    splitBtn.disabled = segments.length === 0;
    segmentStatus.textContent = `Found ${segments.length} segment(s). Drag green handles to adjust, double-click a segment to remove it.`;
  } catch (e) {
    segmentStatus.textContent = `Error: ${e.message}`;
  }
});

addSegmentBtn.addEventListener("click", () => {
  if (!totalDurationMs) return;
  const segmentLengthMs = Number(segmentLengthInput.value);
  const start = Math.max(0, totalDurationMs / 2 - segmentLengthMs / 2);
  segments.push({ startMs: start, endMs: Math.min(totalDurationMs, start + segmentLengthMs) });
  drawWaveform();
  splitBtn.disabled = false;
});

splitBtn.addEventListener("click", async () => {
  if (segments.length === 0) return;
  if (!currentSampleId) {
    segmentStatus.textContent = "Looking up sample on Edge Impulse...";
    try {
      currentSampleId = await lookupSampleId(currentFilename, 3, 1500);
    } catch (e) {
      segmentStatus.textContent = e.message;
      return;
    }
  }
  const ok = confirm(`Split & save ${segments.length} sample(s) to Edge Impulse? The original recording will be deleted there (recoverable via cold storage) and its local copy on the Pi will be removed too.`);
  if (!ok) return;
  splitBtn.disabled = true;
  segmentStatus.textContent = "Splitting...";
  try {
    const res = await fetch(`/api/train/samples/${currentSampleId}/segment`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        segments: segments.map(s => ({ startMs: Math.round(s.startMs), endMs: Math.round(s.endMs) })),
        filename: currentFilename,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    trainStatus.textContent = data.original_deleted
      ? `Split into ${data.split} sample(s) and removed the original recording.`
      : `Split into ${data.split} sample(s), but could not remove the original recording — delete it manually in Edge Impulse Studio to keep it out of training.`;
    loadSampleCounts();
    segmentReviewEl.style.display = "none";
    segments = [];
    currentSampleId = null;
    currentFilename = null;
    audioBuffer = null;
    totalDurationMs = 0;
  } catch (e) {
    segmentStatus.textContent = `Error: ${e.message}`;
    splitBtn.disabled = false;
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

let lastModelAccuracy = null;

async function loadModelMetrics() {
  const res = await fetch("/api/model/metrics");
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "failed to load metrics");
  const metrics = (data.modelValidationMetrics || [])[0];
  if (!metrics) throw new Error("no validation metrics available");
  lastModelAccuracy = metrics.accuracy * 100;
  document.getElementById("model-accuracy").textContent = `${lastModelAccuracy.toFixed(1)}%`;
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
    const res = await fetch("/api/model/activate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ accuracy: lastModelAccuracy }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    const archive = data.archive || {};
    modelStatus.textContent = archive.archived
      ? `New model activated and ei-runner restarted. Archived to git as ${archive.filename}.`
      : `New model activated and ei-runner restarted. Git archive failed: ${archive.error || "unknown error"}`;
  } catch (e) {
    modelStatus.textContent = `Error: ${e.message}`;
    activateBtn.disabled = false;
  }
});

// --- Trigger captures ---

const capturesList = document.getElementById("captures-list");
const capturesStatus = document.getElementById("captures-status");
const captureAudio = document.getElementById("capture-audio");

function parseCaptureFilename(filename) {
  const base = filename.replace(/\.wav$/, "");
  const parts = base.split("-");
  const rest = parts.slice(6); // drop the 6-part YYYY-MM-DD-HH-MM-SS timestamp
  const score = rest[rest.length - 1];
  const label = rest.slice(0, -1).join(" ");
  return { label, score: Number(score) };
}

function playCapture(filename) {
  captureAudio.src = `/trigger-captures/${filename}`;
  captureAudio.play();
}

async function loadCaptures() {
  try {
    const res = await fetch("/api/captures");
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    const captures = data.captures || [];
    if (captures.length === 0) {
      capturesList.innerHTML = `<li class="empty">No captures yet</li>`;
      capturesStatus.textContent = "Captures are saved automatically whenever a command triggers.";
      return;
    }
    capturesStatus.textContent = `${captures.length} capture(s), most recent first.`;
    const labels = window.LABELS || [];
    capturesList.innerHTML = captures.map(c => {
      const { label, score } = parseCaptureFilename(c.filename);
      const pct = Number.isFinite(score) ? `${(score * 100).toFixed(0)}%` : "?";
      const options = labels.map(l => `<option value="${l}">${l}</option>`).join("");
      return `
        <li>
          <span class="capture-row">
            <button class="play-btn" data-filename="${c.filename}" title="Play">&#9654;</button>
            <span>${label} (${pct}) &middot; ${timeAgo(c.mtime)}</span>
          </span>
          <span class="capture-actions">
            <button class="correct" data-filename="${c.filename}" title="Detected label is correct &mdash; upload as ${label}">&check; Correct</button>
            <select class="relabel-select" data-filename="${c.filename}">
              <option value="" selected disabled>Relabel as&hellip;</option>
              ${options}
            </select>
            <button class="discard" data-filename="${c.filename}" title="Not usable for training &mdash; delete without uploading">&cross; Discard</button>
          </span>
        </li>
      `;
    }).join("");
  } catch (e) {
    capturesStatus.textContent = `Could not load captures: ${e.message}`;
  }
}

function disableCaptureRow(el, disabled) {
  const row = el.closest("li");
  if (!row) return;
  row.querySelectorAll("button, select").forEach(x => { x.disabled = disabled; });
}

async function submitCaptureAction(el, filename, url, options) {
  disableCaptureRow(el, true);
  try {
    const res = await fetch(url, options);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    await loadCaptures();
  } catch (e) {
    capturesStatus.textContent = `Error: ${e.message}`;
    disableCaptureRow(el, false);
  }
}

capturesList.addEventListener("click", (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  const filename = btn.dataset.filename;
  if (!filename) return;

  if (btn.classList.contains("play-btn")) {
    playCapture(filename);
    return;
  }

  if (btn.classList.contains("correct")) {
    submitCaptureAction(btn, filename, `/api/captures/${encodeURIComponent(filename)}/confirm`, { method: "POST" });
    return;
  }

  if (btn.classList.contains("discard")) {
    submitCaptureAction(btn, filename, `/api/captures/${encodeURIComponent(filename)}`, { method: "DELETE" });
  }
});

capturesList.addEventListener("change", (e) => {
  const select = e.target.closest(".relabel-select");
  if (!select) return;
  const filename = select.dataset.filename;
  const label = select.value;
  if (!filename || !label) return;
  submitCaptureAction(select, filename, `/api/captures/${encodeURIComponent(filename)}/relabel`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ label }),
  });
});

// --- Audio pipeline mode (Classic / Buffer) ---

const audioModeStatus = document.getElementById("audio-mode-status");
const audioModeClassicBtn = document.getElementById("audio-mode-classic");
const audioModeBufferBtn = document.getElementById("audio-mode-buffer");
const capturesCard = document.getElementById("captures-card");

function setAudioModeButtons(mode) {
  audioModeClassicBtn.classList.toggle("primary", mode === "classic");
  audioModeBufferBtn.classList.toggle("primary", mode === "buffer");
  capturesCard.style.display = mode === "buffer" ? "" : "none";
}

async function loadAudioMode() {
  try {
    const res = await fetch("/api/audio-mode");
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    setAudioModeButtons(data.mode);
    audioModeStatus.textContent = data.mode === "buffer" ? "Currently On" : "Currently Off";
  } catch (e) {
    audioModeStatus.textContent = `Could not load audio mode: ${e.message}`;
  }
}

async function switchAudioMode(mode) {
  const label = mode === "buffer" ? "On" : "Off";
  const ok = confirm(
    `Turn Audio Capture Mode ${label}? This restarts voice detection and can take up to a minute, ` +
    `with voice control briefly unavailable during the switch. If it fails to come back up, it will ` +
    `automatically roll back to Off.`
  );
  if (!ok) return;

  audioModeClassicBtn.disabled = true;
  audioModeBufferBtn.disabled = true;
  audioModeStatus.textContent = `Switching to ${label}... this can take up to a minute.`;

  try {
    const res = await fetch("/api/audio-mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode }),
    });
    const data = await res.json();
    if (!res.ok) {
      audioModeStatus.textContent = data.rolled_back
        ? `Error: ${data.error} (automatically rolled back to Off)`
        : `Error: ${data.error}`;
    } else {
      audioModeStatus.textContent = `Switched ${data.mode === "buffer" ? "On" : "Off"} successfully.`;
    }
  } catch (e) {
    audioModeStatus.textContent = `Error: ${e.message}`;
  } finally {
    audioModeClassicBtn.disabled = false;
    audioModeBufferBtn.disabled = false;
    await loadAudioMode();
  }
}

audioModeClassicBtn.addEventListener("click", () => switchAudioMode("classic"));
audioModeBufferBtn.addEventListener("click", () => switchAudioMode("buffer"));

// --- Sonos transport controls ---

const playPauseBtn = document.getElementById("sonos-play-pause");
const muteBtn = document.getElementById("sonos-mute");
const volumeSlider = document.getElementById("sonos-volume");

const ICON_UNMUTED = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="3 9 3 15 8 15 13 20 13 4 8 9 3 9" fill="currentColor" stroke="none"/><path d="M16 8a5 5 0 0 1 0 8"/><path d="M18.5 5.5a9 9 0 0 1 0 13"/></svg>';
const ICON_MUTED = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="3 9 3 15 8 15 13 20 13 4 8 9 3 9" fill="currentColor" stroke="none"/><line x1="16" y1="9" x2="22" y2="15"/><line x1="22" y1="9" x2="16" y2="15"/></svg>';

let sonosIsPlaying = false;
let sonosIsMuted = false;
let volumeSliderActive = false;

async function pollSonos() {
  try {
    const res = await fetch("/api/sonos/state");
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    sonosIsPlaying = data.state === "playing";
    sonosIsMuted = data.is_volume_muted;
    playPauseBtn.innerHTML = sonosIsPlaying ? "&#9208;" : "&#9654;";
    muteBtn.innerHTML = sonosIsMuted ? ICON_MUTED : ICON_UNMUTED;
    if (!volumeSliderActive && typeof data.volume_level === "number") {
      volumeSlider.value = data.volume_level;
    }
  } catch (e) {
    // Stay quiet on transient HA polling errors; the badges already surface connectivity issues.
  }
}

playPauseBtn.addEventListener("click", async () => {
  playPauseBtn.disabled = true;
  try {
    await fetch(sonosIsPlaying ? "/api/sonos/pause" : "/api/sonos/play", { method: "POST" });
    await pollSonos();
  } finally {
    playPauseBtn.disabled = false;
  }
});

muteBtn.addEventListener("click", async () => {
  muteBtn.disabled = true;
  try {
    await fetch("/api/sonos/mute", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ muted: !sonosIsMuted }),
    });
    await pollSonos();
  } finally {
    muteBtn.disabled = false;
  }
});

volumeSlider.addEventListener("input", () => { volumeSliderActive = true; });
volumeSlider.addEventListener("change", async () => {
  try {
    await fetch("/api/sonos/volume", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ level: Number(volumeSlider.value) }),
    });
  } finally {
    volumeSliderActive = false;
  }
});

pollState();
pollSystem();
pollSonos();
loadSampleCounts();
loadCaptures();
loadAudioMode();
setInterval(pollState, 1000);
setInterval(pollSystem, 5000);
setInterval(pollSonos, 5000);
setInterval(loadCaptures, 5000);
