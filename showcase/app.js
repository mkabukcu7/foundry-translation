"use strict";
const $ = id => document.getElementById(id);
let selection = null;
let busy = false;
let viewing = null;
let reviewEnabled = true;
const stages = ["validate", "extract", "prepare", "translate", "reconstruct", "check", "save"];
const stageNames = {validate:"Validating document", extract:"Extracting text / OCR", prepare:"Selecting glossary terms", translate:"Running selected translation engine", reconstruct:"Rebuilding translated document", check:"Independent reviewers are checking", save:"Saving local results"};
function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}
async function api(url, options, onReconnect) {
  const response = onReconnect ? await fetchJobStatus(url, onReconnect) : await fetch(url, options);
  if (!response.ok) {
    let message = `Request failed (HTTP ${response.status})`;
    try { const body = await response.json(); if (typeof body.detail === "string") message = body.detail; } catch { /* Preserve HTTP status if the server response is not JSON. */ }
    throw new Error(message);
  }
  return response.json();
}
function error(message) { $("error").textContent = message; $("error").hidden = false; }
function choose(value) {
  selection = value;
  document.querySelectorAll(".sample").forEach(button => {
    const selected = button.dataset.id === value?.sample;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
  $("run").disabled = busy || !value;
}
function setBusy(value) {
  busy = value;
  $("run").disabled = value || !selection;
  $("upload").disabled = value;
  $("source-language").disabled = value;
  $("target-language").disabled = value;
  $("strategy").disabled = value;
  $("reasoning-effort").disabled = value;
  $("glossary-mode").disabled = value;
  $("hybrid").disabled = value || !reviewEnabled;
  document.querySelectorAll(".sample, .history button").forEach(button => { button.disabled = value || button.dataset.unavailable === "true"; });
  $("run").textContent = value ? "Workflow running..." : reviewEnabled ? "Translate & review" : "Translate (no review)";
}
function progress(stage, finished = false) {
  const index = stages.indexOf(stage);
  document.querySelectorAll("#stages li").forEach(node => {
    const position = stages.indexOf(node.dataset.stage);
    node.classList.toggle("done", finished || position < index);
    node.classList.toggle("active", !finished && position === index);
  });
  $("workflow-title").textContent = finished ? "Workflow complete" : (stageNames[stage] || "Running workflow");
  $("run-state").textContent = finished ? "FINISHED" : "LIVE";
}
function rows(id, values) {
  $(id).replaceChildren(...values.map(values => {
    const row = el("tr");
    values.forEach(value => row.append(el("td", value ?? "\u2014")));
    return row;
  }));
}
function table(headers, values) {
  const wrap = el("div", undefined, "table-wrap");
  const t = el("table"), head = el("thead"), row = el("tr"), body = el("tbody");
  headers.forEach(h => row.append(el("th", h))); head.append(row); t.append(head);
  values.forEach(values => { const r = el("tr"); values.forEach(v => r.append(el("td", v ?? "\u2014"))); body.append(r); });
  t.append(body); wrap.append(t); return wrap;
}
function render(data) {
  const r = data.report, preview = data.preview, cost = r.costSignals || {};
  const flagged = r.status !== "complete";
  const unreviewed = r.status === "not_reviewed";
  $("empty").hidden = true; $("result").hidden = false;
  $("verdict").textContent = unreviewed ? "Translation complete - not reviewed" : flagged ? "Human review required" : "Checks passed";
  $("quality-badge").textContent = unreviewed ? "NOT REVIEWED" : flagged ? "NEEDS REVIEW" : "PASSED";
  $("quality-badge").className = `badge ${flagged ? "review" : "good"}`;
  $("verdict-note").textContent = unreviewed ? "Quality review was skipped. This translation is not approved." : flagged
    ? "The workflow completed. Its checks identified concerns or reviewer disagreement; this document is not approved."
    : "Configured checks passed. This is a demo result, not a guarantee of translation accuracy.";
  $("job-id").textContent = `jobId / ${data.jobId} / Input: ${r.sourceFile || "not recorded"} / SHA-256: ${r.sourceSha256 || "not recorded"}`;
  const translation = r.translation || {};
  const engine = translation.engineDetails || {};
  const engineLabels = {
    translator:"Azure Translator NMT", doc_llm:"Document Translator LLM",
    gpt6_luna:"GPT-6 Luna", gpt6_1_terra:"GPT-6.1 Terra", gpt5_6_luna:"GPT-5.6 Luna baseline",
    foundry_agent:"Pinned Foundry agent"
  };
  $("strategy-title").textContent = engineLabels[engine.engine] || "Engine not recorded";
  $("strategy-details").textContent = [
    engine.maturity,
    engine.model && `model ${engine.model}`,
    engine.agent && `agent ${engine.agent}`,
    engine.agentVersion && `version ${engine.agentVersion}`,
    engine.deploymentName && `deployment ${engine.deploymentName}`,
    engine.deploymentType,
    engine.apiVersion && `API ${engine.apiVersion}`,
    engine.reasoningEffort && `reasoning ${engine.reasoningEffort}`,
    translation.durationSeconds != null && `${translation.durationSeconds.toFixed(2)} seconds`
  ].filter(Boolean).join(" \u00b7 ") || "Engine metadata unavailable.";
  $("fidelity-details").textContent = r.documentFidelity?.evaluated
    ? "Document fidelity was evaluated separately."
    : `Document fidelity not evaluated: ${(r.documentFidelity?.concerns || []).join(", ")}.`;
  const agreements = r.checkerAgreement || [];
  const matched = agreements.reduce((n, a) => n + a.matchedIssues, 0);
  const total = agreements.reduce((n, a) => n + a.totalIssues, 0);
  const metrics = [
    [unreviewed ? "Not reviewed" : `${r.score}/100`, "Experimental workflow score"],
    [total ? `${matched}/${total}` : "\u2014", total ? "Exact issue agreement" : "No issues to compare"],
    [cost.charactersCharged == null ? "Unknown" : cost.charactersCharged.toLocaleString(), "Characters charged"],
    [translation.inputTokens?.toLocaleString() ?? "Not reported", "Translation input tokens"],
    [translation.outputTokens?.toLocaleString() ?? "Not reported", "Translation output tokens"],
    [cost.tokens == null ? "Unknown" : cost.tokens.toLocaleString(), "Total recorded tokens"],
    [cost.estimatedCostUSD == null ? "Unknown" : `$${cost.estimatedCostUSD.toFixed(4)}`, "Estimated document cost"]
  ];
  if (r.hybridRouting?.enabled) {
    metrics.push(
      [`${r.hybridRouting.escalatedChunks}/${r.hybridRouting.eligibleChunks}`, "Hybrid chunks escalated"],
      [`${(r.hybridRouting.escalationRate * 100).toFixed(1)}%`, "Hybrid escalation rate"],
      [r.hybridRouting.estimatedCostUSD == null ? "Unknown" : `$${r.hybridRouting.estimatedCostUSD.toFixed(4)}`, "Escalation cost"]
    );
  }
  $("metrics").replaceChildren(...metrics.map(([value, label]) => {
    const node = el("div", undefined, "metric"); node.append(el("strong", value), el("span", label)); return node;
  }));
  $("download").href = `/api/jobs/${data.jobId}/download/${data.output}`;
  $("download-text").href = `/api/jobs/${data.jobId}/download/translated.txt`;
  $("report").href = `/api/jobs/${data.jobId}/download/report.json`;
  $("result-limitations").replaceChildren(...(r.limitations || []).map(text => el("li", text)));
  $("source-text").textContent = preview?.sourceText || "Text preview was not saved for this earlier CLI run.";
  $("source-heading").textContent = `${({it:"Italian", es:"Spanish", en:"English"})[r.sourceLanguage] || "Original"} source`;
  $("target-heading").textContent = `${({it:"Italian", es:"Spanish", en:"English"})[r.targetLanguage] || "Target"} translation`;
  $("target-text").textContent = preview?.translatedText || "Download the translation to inspect this earlier result.";
  $("preview-note").textContent = "See limitations for reconstruction and layout differences. For PowerPoint, text from pictures is included only in the companion TXT. Document fidelity is not assessed.";
  rows("terms", (preview?.terms || []).map(t => [t.term, t.action.toUpperCase(), t.action === "keep" ? t.term : t.target, t.type]));
  $("terms-note").textContent = preview ? `${preview.terms.length} protected / mapped terms. ${r.translation?.glossaryApplied ? "Glossary submitted to Document Translation." : "Terms checked by reviewers; not applied by Translator Text."}` : "Term preview was not saved for this earlier CLI run. Preservation checks are available below.";
  $("reviewers").replaceChildren(...(r.passB || []).map((review, i) => {
    const usage = (r.versions || [])[i] || {};
    const card = el("article", undefined, "reviewer");
    card.append(el("h3", `${usage.agent === "translation-checker" ? "Primary checker" : "Independent checker"} / ${review.score}/100`));
    const primary = usage.agent === "translation-checker";
    card.append(el("p", `${usage.model || "Model not recorded"} \u00b7 ${primary ? "Foundry prompt agent" : "Direct model review"}`, "model"));
    if (usage.responseModel) card.append(el("p", `Reported model: ${usage.responseModel}`, "fine"));
    card.append(el("p", `${primary ? "Agent version" : "Prompt fingerprint"}: ${usage.version || "not recorded"}`, "fine"));
    card.append(el("p", `${review.issues.length} findings \u00b7 ${usage.inputTokens ?? "?"} input / ${usage.outputTokens ?? "?"} output tokens`, "fine"));
    if (!review.issues.length) card.append(el("p", "No issues reported by this reviewer.", "muted"));
    review.issues.forEach(issue => {
      const item = el("div", undefined, "issue");
      item.append(el("span", issue.severity, "severity"), el("h4", issue.type.replaceAll("_", " ")),
        el("p", issue.location, "mono"), el("blockquote", `Source: ${issue.source_excerpt}`),
        el("blockquote", `Target: ${issue.target_excerpt}`), el("p", issue.explanation));
      card.append(item);
    });
    return card;
  }));
  $("agreement").replaceChildren();
  if (!agreements.length) $("agreement").append(el("p", unreviewed ? "Quality review was skipped for this run." : "Secondary reviewer was not enabled for this run.", "fine"));
  agreements.forEach(a => {
    $("agreement").append(el("p", `${a.location} / part ${a.part}: ${a.disagreement ? "issue disagreement" : "no issue disagreement"}; reviewer decisions ${a.decisionAgreement ? "agree" : "differ"}.`, "fine"));
    if (a.issues.length) $("agreement").append(table(["Issue evidence (exact-match key)", "Reviewers"], a.issues.map(i => [i.key.join(" / "), i.reviewers.join(", ")])));
  });
  rows("checks", (r.passA || []).map(f => [f.item, f.type, f.match, f.side]));
  const translationCalls = [
    ...(translation.calls || []).map(u => ({...u, label: `Translation batch ${u.group}`})),
    ...(translation.escalationUsage || []).flatMap((chunk, index) =>
      (chunk.calls || []).map(u => ({...u, label: `Escalation ${index + 1}, batch ${u.group}`}))),
  ];
  const calls = [...(r.prepareUsage || []), ...translationCalls, ...(r.versions || [])];
  $("usage").replaceChildren(table(["Call", "Deployment", "Input tokens", "Output tokens", "Seconds"], calls.map(u => [u.label || u.agent, u.model, u.inputTokens, u.outputTokens, u.durationSeconds?.toFixed(2)])));
  $("usage").append(table(["Step", "Seconds"], Object.entries(cost.durationsSeconds || {}).map(([k,v]) => [k, v.toFixed(2)])));
  $("usage").append(el("p", `OCR pages: ${cost.ocrPages ?? "unknown"} / Images translated: ${cost.imagesTranslated ?? "unknown"} / Charged characters: ${cost.charactersCharged ?? "unknown"}. No dollar estimate is inferred.`, "fine"));
}
async function openJob(id) {
  $("error").hidden = true;
  const data = await api(`/api/jobs/${id}`, undefined, () => {
    $("progress-note").textContent = `jobId / ${id} · Reconnecting to the local showcase. No new job was submitted.`;
  });
  if (data.state === "failed") {
    $("workflow-title").textContent = "Workflow stopped";
    $("run-state").textContent = "FAILED";
    error(`${data.error} jobId / ${id}`);
    if (data.partialDocument === "translated-unreviewed.pptx") {
      const link = el("a", "Download PowerPoint draft (not reviewed)");
      link.href = `/api/jobs/${id}/download/translated-unreviewed.pptx`;
      $("error").append(el("br"), link);
    }
    if (data.partialOutput === "translated.txt") {
      const link = el("a", "Download translated text (not reviewed)");
      link.href = `/api/jobs/${id}/download/translated.txt`;
      $("error").append(el("br"), link);
    }
    return true;
  }
  if (data.state === "finished") {
    progress("save", true); render(data);
    $("progress-note").textContent = "Saved local result. Download the report for the full evidence and per-call usage.";
    return true;
  }
  progress(data.stage);
  $("progress-note").textContent = `jobId / ${id} \u00b7 Azure calls can take several minutes. Closing this page does not cancel an in-flight call.`;
  return false;
}
async function history() {
  const jobs = await api("/api/jobs");
  $("history").replaceChildren(...jobs.map(job => {
    const button = el("button");
    button.append(el("span", job.status === "not_reviewed" ? "Not reviewed" : `${job.status === "complete" ? "Checks passed" : "Needs review"} \u00b7 ${job.score}/100`),
      el("small", `${new Date(job.savedAt * 1000).toLocaleString()} / ${job.jobId.slice(0, 8)}`));
    button.disabled = busy;
    button.addEventListener("click", async () => {
      if (busy) return;
      viewing = job.jobId;
      try { await openJob(job.jobId); } catch (e) { error(e.message); }
    });
    return button;
  }));
  if (!jobs.length) $("history").append(el("p", "No saved runs yet.", "fine"));
}
$("upload").addEventListener("change", () => {
  const file = $("upload").files[0];
  $("file-name").textContent = file ? file.name : "No file selected";
  choose(file ? {file} : null);
});
$("run").addEventListener("click", async () => {
  if (busy || !selection) return;
  const input = selection;
  $("error").hidden = true; $("result").hidden = true; $("empty").hidden = false;
  setBusy(true);
  try {
    if (input.file) {
      const suffix = input.file.name.split(".").pop().toLowerCase();
      const max = ["pptx", "docx", "xlsx"].includes(suffix) ? 10 : 40;
      if (!/\.(pdf|pptx|docx|xlsx|jpe?g|png|bmp|tiff?)$/i.test(input.file.name) || input.file.size > max * 1024 * 1024)
        throw new Error("Choose a PDF or supported image up to 40 MiB, or a PPTX/DOCX/XLSX up to 10 MiB.");
    }
    const strategy = $("strategy").value;
    if ($("source-language").value === $("target-language").value)
      throw new Error("Choose different source and target languages.");
    const query = `${input.sample ? `sample=${encodeURIComponent(input.sample)}` : `filename=${encodeURIComponent(input.file.name)}`}&engine=${encodeURIComponent(strategy)}&sourceLanguage=${encodeURIComponent($("source-language").value)}&targetLanguage=${encodeURIComponent($("target-language").value)}&reasoningEffort=${encodeURIComponent($("reasoning-effort").value)}&glossaryMode=${encodeURIComponent($("glossary-mode").value)}&hybrid=${$("hybrid").checked}`;
    viewing = null;
    const job = await api(`/api/jobs?${query}`, {method:"POST", headers:{"X-Showcase-Request":"1", "Content-Type":"application/octet-stream"}, body:input.file || null});
    viewing = job.jobId;
    while (viewing === job.jobId) {
      if (await openJob(job.jobId)) break;
      await new Promise(resolve => setTimeout(resolve, 1200));
    }
    await history();
  } catch (e) {
    error(`${e.message}${viewing ? ` / jobId ${viewing}` : ""}`);
    $("workflow-title").textContent = "Unable to complete request";
    $("run-state").textContent = "ERROR";
  } finally { setBusy(false); }
});
async function init() {
  try {
    const config = await api("/api/config");
    reviewEnabled = config.reviewEnabled;
    if (!reviewEnabled) {
      document.querySelector('#stages li[data-stage="check"]').hidden = true;
      $("hybrid").checked = false;
      $("workflow-title").textContent = "Manual review";
      $("progress-note").textContent = "Translations are saved without automated review for you to inspect.";
      setBusy(false);
    }
    const samples = await api("/api/samples");
    $("samples").replaceChildren(...samples.map(sample => {
      const button = el("button", undefined, "sample"); button.dataset.id = sample.id;
      button.dataset.unavailable = String(!sample.available); button.disabled = !sample.available;
      const label = el("span"); label.append(el("strong", sample.title), el("small", sample.available ? sample.description : "Generate samples with validation task"));
      button.append(el("span", sample.id === "slides" ? "PPTX" : "PDF", "file-icon"), label);
      button.addEventListener("click", () => { if (!busy) { $("upload").value = ""; $("file-name").textContent = "Synthetic sample selected"; choose({sample:sample.id}); } });
      return button;
    }));
    const first = samples.find(s => s.available);
    if (first) choose({sample:first.id});
    await history();
  } catch(e) { error(e.message); }
}
init();
