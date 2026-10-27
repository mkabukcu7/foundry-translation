"use strict";
const $ = id => document.getElementById(id);
let selection = null, busy = false;
function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}
async function api(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let message = `Request failed (HTTP ${response.status})`;
    try { const body = await response.json(); if (typeof body.detail === "string") message = body.detail; } catch { /* Keep the HTTP status when the response is not JSON. */ }
    throw new Error(message);
  }
  return response.json();
}
function showError(message) { $("error").textContent = message; $("error").hidden = false; }
function choose(value) {
  selection = value;
  document.querySelectorAll(".sample").forEach(button => {
    const selected = button.dataset.id === value?.sample;
    button.classList.toggle("selected", selected); button.setAttribute("aria-pressed", String(selected));
  });
  $("run").disabled = busy || !value;
}
function setBusy(value) {
  busy = value; $("run").disabled = value || !selection; $("upload").disabled = value;
  $("run").textContent = value ? "Translating..." : "Translate only";
  document.querySelectorAll(".sample, .history button").forEach(b => { b.disabled = value || b.dataset.unavailable === "true"; });
}
function render(data) {
  const r = data.report, cost = r.costSignals;
  $("empty").hidden = true; $("result").hidden = false;
  $("job-id").textContent = `jobId / ${data.jobId}\nInput: ${r.sourceFile || "not recorded"} / SHA-256: ${r.sourceSha256 || "not recorded"}`;
  $("source-text").textContent = data.preview?.sourceText || "Preview not available.";
  $("target-text").textContent = data.preview?.translatedText || "Preview not available.";
  const seconds = Object.values(cost.durationsSeconds).reduce((a,b) => a+b, 0);
  const values = [[cost.charactersCharged ?? "Unknown", "Characters charged"], [cost.ocrPages, "OCR pages"], [0, "Agent tokens (no calls)"], [`${seconds.toFixed(1)}s`, "Workflow time"]];
  $("metrics").replaceChildren(...values.map(([value, label]) => {
    const n = el("div", undefined, "metric"); n.append(el("strong", String(value)), el("span", label)); return n;
  }));
  $("limitations").replaceChildren(...r.limitations.map(t => el("li", t)));
  $("timings").replaceChildren(...Object.entries(cost.durationsSeconds).map(([k,v]) => el("p", `${k}: ${v.toFixed(2)} seconds`)));
  $("download").href = `/api/jobs/${data.jobId}/download/${data.output}`;
  $("report").href = `/api/jobs/${data.jobId}/download/report.json`;
}
async function openJob(id) {
  const data = await api(`/api/jobs/${id}`);
  const finished = data.state === "finished";
  const stages = ["validate", "extract", "translate", "save"];
  document.querySelectorAll("#stages li").forEach(n => {
    n.classList.toggle("done", finished || stages.indexOf(n.dataset.stage) < stages.indexOf(data.stage));
    n.classList.toggle("active", !finished && n.dataset.stage === data.stage);
  });
  $("progress-note").textContent = `jobId / ${id} \u00b7 No agents called. Closing this page does not cancel in-flight calls.`;
  if (data.state === "failed") {
    showError(`${data.error} / jobId ${id}`); $("workflow-title").textContent = "Workflow stopped"; $("run-state").textContent = "FAILED"; return true;
  }
  $("workflow-title").textContent = finished ? "Baseline workflow complete" : ({validate:"Validating", extract:"Extracting text / OCR", translate:"Translator is translating", save:"Saving local result"}[data.stage] || "Running");
  $("run-state").textContent = finished ? "FINISHED" : "LIVE";
  if (finished) render(data);
  return finished;
}
async function history() {
  const jobs = await api("/api/jobs");
  $("history").replaceChildren(...jobs.map(job => {
    const b = el("button"); b.append(el("span", "Translated / not reviewed"), el("small", `${new Date(job.savedAt * 1000).toLocaleString()} / ${job.jobId.slice(0,8)}`)); b.disabled = busy;
    b.addEventListener("click", async () => { if (!busy) { $("error").hidden = true; try { await openJob(job.jobId); } catch(e) { showError(e.message); } } }); return b;
  }));
}
$("upload").addEventListener("change", () => {
  const file = $("upload").files[0]; $("file-name").textContent = file?.name || "No file selected"; choose(file ? {file} : null);
});
$("run").addEventListener("click", async () => {
  if (busy || !selection) return;
  const input = selection; setBusy(true); $("error").hidden = true; $("result").hidden = true; $("empty").hidden = false;
  try {
    const query = input.sample ? `sample=${encodeURIComponent(input.sample)}` : `filename=${encodeURIComponent(input.file.name)}`;
    const job = await api(`/api/jobs?${query}`, {method:"POST", headers:{"X-Showcase-Request":"1","Content-Type":"application/octet-stream"}, body:input.file || null});
    while (!await openJob(job.jobId)) await new Promise(resolve => setTimeout(resolve,1200));
    await history();
  } catch(e) { showError(e.message); $("workflow-title").textContent = "Request failed"; $("run-state").textContent = "ERROR"; }
  finally { setBusy(false); }
});
async function init() {
  try {
    const samples = await api("/api/samples");
    $("samples").replaceChildren(...samples.map(s => {
      const b = el("button", undefined, "sample"); b.dataset.id = s.id; b.dataset.unavailable = String(!s.available); b.disabled = !s.available;
      const text = el("span"); text.append(el("strong", s.title), el("small", s.available ? s.description : "Generate samples first"));
      b.append(el("span", s.id === "slides" ? "PPTX" : "PDF", "file-icon"),text);
      b.addEventListener("click", () => { if (!busy) { $("upload").value = ""; $("file-name").textContent = "Synthetic sample selected"; choose({sample:s.id}); } }); return b;
    }));
    const first = samples.find(s => s.available); if (first) choose({sample:first.id}); await history();
  } catch(e) { showError(e.message); }
}
init();
