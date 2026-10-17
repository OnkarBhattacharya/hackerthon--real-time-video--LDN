const queryEl = document.getElementById("query");
const investigateBtn = document.getElementById("investigate");
const statusEl = document.getElementById("status");
const stepsEl = document.getElementById("steps");
const errorEl = document.getElementById("error");
const skippedEl = document.getElementById("skipped");
const emptyEl = document.getElementById("empty");
const resultsEl = document.getElementById("results");

const DEFAULT_QUERY = "person close to a moving vehicle";

const SEVERITY = {
  1: "none",
  2: "distant",
  3: "close",
  4: "evasive",
  5: "contact",
};

const ROLES = [
  ["previous", "Before"],
  ["event", "Event"],
  ["next", "After"],
];

let pollToken = 0;

function apiUrl(path) {
  const relative = String(path || "").replace(/^\//, "");
  let dir = window.location.pathname;
  const leaf = dir.split("/").pop() || "";
  if (!dir.endsWith("/")) {
    if (/\.[a-z0-9]+$/i.test(leaf)) {
      dir = dir.slice(0, dir.lastIndexOf("/") + 1);
    } else {
      dir += "/";
    }
  }
  return dir + relative;
}

function queryText() {
  const text = queryEl.value.trim();
  return text || DEFAULT_QUERY;
}

function sleep(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function statusLine(health) {
  const parts = [];
  if (typeof health.indexed_segments === "number") {
    parts.push(health.indexed_segments + " indexed segments");
  }
  if (health.generated_at) {
    parts.push("last run " + health.generated_at);
  } else if (parts.length) {
    parts.push("no investigation has been run in this session");
  }
  if (health.features && health.features.fixture) {
    parts.push("fixture data, not the live archive");
  }
  if (!parts.length) {
    return "The archive has not been queried in this session.";
  }
  return parts.join(" · ");
}

async function loadStatus() {
  try {
    const response = await fetch(apiUrl("api/health"));
    const health = await response.json();
    statusEl.textContent = statusLine(health);
  } catch (_err) {
    statusEl.textContent = "The archive has not been queried in this session.";
  }
}

function clearNode(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function showError(error, hint) {
  errorEl.hidden = false;
  clearNode(errorEl);
  const title = document.createElement("p");
  title.textContent = error || "The investigation failed.";
  const next = document.createElement("p");
  const hintText = String(hint || "").trim();
  const retry = "You can run Investigate again.";
  if (!hintText) {
    next.textContent = retry;
  } else if (hintText.toLowerCase().indexOf("investigate again") === -1) {
    next.textContent = hintText + " " + retry;
  } else {
    next.textContent = hintText;
  }
  errorEl.appendChild(title);
  errorEl.appendChild(next);
}

function hideError() {
  errorEl.hidden = true;
  clearNode(errorEl);
}

function renderSteps(steps) {
  clearNode(stepsEl);
  for (const step of steps) {
    const item = document.createElement("li");
    item.textContent = step;
    stepsEl.appendChild(item);
  }
}

function skippedCameras(job) {
  const found = [];
  const seen = new Set();
  const add = (name) => {
    const id = String(name || "").trim();
    if (!id || seen.has(id)) return;
    seen.add(id);
    found.push(id);
  };
  for (const id of job.skipped_cameras || []) add(id);
  for (const step of job.steps || []) {
    if (step.indexOf("Skipping camera ") === 0) {
      add(step.slice("Skipping camera ".length).split(";")[0]);
    }
  }
  return found;
}

function renderSkipped(job) {
  const names = skippedCameras(job);
  if (!names.length) {
    skippedEl.hidden = true;
    skippedEl.textContent = "";
    return;
  }
  skippedEl.hidden = false;
  skippedEl.textContent = names
    .map((id) => id + " is not indexed and was skipped.")
    .join(" ");
}

function safeClipUrl(url) {
  if (typeof url !== "string" || url.indexOf("api/") !== 0) return "";
  return apiUrl(url);
}

function addSlot(parent, label, clip, role, missing) {
  const slot = document.createElement("div");
  slot.className = "slot" + (role === "event" ? " event" : "");
  const heading = document.createElement("p");
  heading.textContent = label;
  slot.appendChild(heading);
  if (missing || !clip || !clip.clip_url) {
    const note = document.createElement("p");
    note.className = "missing";
    note.textContent = missing
      ? "No " + label.toLowerCase() + " segment for this video."
      : "No playable " + label.toLowerCase() + " clip was returned.";
    slot.appendChild(note);
  } else {
    const video = document.createElement("video");
    video.controls = true;
    video.playsInline = true;
    video.preload = "metadata";
    video.src = safeClipUrl(clip.clip_url);
    slot.appendChild(video);
  }
  parent.appendChild(slot);
}

function showPanel(card, title, text, filename, mime) {
  let panel = card.querySelector(".panel");
  if (!panel) {
    panel = document.createElement("div");
    panel.className = "panel";
    card.appendChild(panel);
  }
  clearNode(panel);
  const heading = document.createElement("h3");
  heading.textContent = title;
  const pre = document.createElement("pre");
  pre.textContent = text;
  const row = document.createElement("div");
  row.className = "actions";
  const copyBtn = document.createElement("button");
  copyBtn.type = "button";
  copyBtn.textContent = "Copy";
  copyBtn.addEventListener("click", async () => {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
        copyBtn.textContent = "Copied";
        return;
      }
    } catch (_err) {
      /* Fall through to the selection copy below. */
    }
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    document.body.appendChild(area);
    area.select();
    let copied = false;
    try {
      copied = document.execCommand("copy");
    } catch (_err) {
      copied = false;
    }
    area.remove();
    copyBtn.textContent = copied ? "Copied" : "Select the text to copy";
  });
  const downloadBtn = document.createElement("button");
  downloadBtn.type = "button";
  downloadBtn.textContent = "Download";
  downloadBtn.addEventListener("click", () => download(filename, text, mime));
  row.appendChild(copyBtn);
  row.appendChild(downloadBtn);
  panel.appendChild(heading);
  panel.appendChild(pre);
  panel.appendChild(row);
}

function download(filename, text, mime) {
  const blob = new Blob([text], { type: mime });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function safeName(value) {
  return String(value || "hit").replace(/[^a-zA-Z0-9._-]+/g, "-").slice(0, 80);
}

function renderCard(hit) {
  const card = document.createElement("article");
  card.className = "card";
  const severity = Number(hit.severity) || 1;
  const badge = document.createElement("p");
  const badgeText = document.createElement("span");
  badgeText.className = "badge badge-" + severity;
  badgeText.textContent = severity + " " + (SEVERITY[severity] || "unscored");
  badge.appendChild(badgeText);
  card.appendChild(badge);
  if (hit.rule_based) {
    const caption = document.createElement("p");
    caption.className = "caption";
    caption.textContent = "Rule-based score; this did not come from the model.";
    card.appendChild(caption);
  }
  const rationale = document.createElement("p");
  rationale.className = "rationale";
  rationale.textContent = hit.rationale || "";
  const meta = document.createElement("p");
  meta.className = "meta";
  meta.textContent = (hit.camera_id || "camera") + " · " + hit.start + "–" + hit.end + " s";
  card.appendChild(rationale);
  card.appendChild(meta);

  const triptych = document.createElement("div");
  triptych.className = "triptych";
  const missing = new Set(hit.missing_neighbours || []);
  const clips = hit.clips || {};
  for (const [role, label] of ROLES) {
    addSlot(triptych, label, clips[role], role, missing.has(role));
  }
  card.appendChild(triptych);

  if (hit.clip_error) {
    const clipError = document.createElement("p");
    clipError.textContent = hit.clip_error;
    card.appendChild(clipError);
  }
  if (hit.crop_url) {
    const image = document.createElement("img");
    image.className = "crop";
    image.alt = "Object crop";
    image.src = safeClipUrl(hit.crop_url) || hit.crop_url;
    card.appendChild(image);
  }

  const privacy = document.createElement("div");
  privacy.className = "privacy";
  const flags = document.createElement("p");
  flags.textContent = (hit.privacy_flags || []).join("; ") || "No extra privacy flags.";
  const note = document.createElement("p");
  note.textContent = hit.privacy_note || "Review before sharing. Possible personal data (GDPR).";
  privacy.appendChild(flags);
  privacy.appendChild(note);
  card.appendChild(privacy);

  const actions = document.createElement("div");
  actions.className = "actions";
  actions.appendChild(actionButton(card, hit, "Report (English)", "en"));
  actions.appendChild(actionButton(card, hit, "Bericht (Deutsch)", "de"));
  const ticketBtn = document.createElement("button");
  ticketBtn.type = "button";
  ticketBtn.textContent = "Create ticket";
  ticketBtn.addEventListener("click", () => openTicket(card, hit));
  actions.appendChild(ticketBtn);
  card.appendChild(actions);
  return card;
}

function actionButton(card, hit, label, lang) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.addEventListener("click", () => openReport(card, hit, lang));
  return button;
}

async function openReport(card, hit, lang) {
  const title = lang === "de" ? "Bericht (Deutsch)" : "Report (English)";
  const filename = safeName(hit.segment_id) + "-" + lang + ".txt";
  try {
    const response = await fetch(
      apiUrl("api/hits/report?lang=" + encodeURIComponent(lang) + "&segment_id=" + encodeURIComponent(hit.segment_id)),
      { method: "POST" }
    );
    const body = await response.json();
    if (!response.ok) {
      showPanel(card, title, (body.error || "Report failed.") + "\n" + (body.hint || ""), filename, "text/plain");
      return;
    }
    const heading = title + (body.source === "template" && lang === "de" ? " · template" : "");
    showPanel(card, heading, body.text || "", filename, "text/plain");
  } catch (_err) {
    showPanel(card, title, "The report request failed.\nCheck that the server is running and try again.", filename, "text/plain");
  }
}

async function openTicket(card, hit) {
  const filename = safeName(hit.segment_id) + "-ticket.json";
  try {
    const response = await fetch(
      apiUrl("api/hits/ticket?segment_id=" + encodeURIComponent(hit.segment_id)),
      { method: "POST" }
    );
    const body = await response.json();
    if (!response.ok) {
      showPanel(card, "Create ticket", (body.error || "Ticket failed.") + "\n" + (body.hint || ""), filename, "application/json");
      return;
    }
    const text = JSON.stringify(body, null, 2);
    const title = body.posted ? "Ticket posted" : "Ticket payload";
    showPanel(card, title, text, filename, "application/json");
  } catch (_err) {
    showPanel(card, "Create ticket", "The ticket request failed.\nCheck that the server is running and try again.", filename, "application/json");
  }
}

function renderHits(hits) {
  clearNode(resultsEl);
  if (!hits.length) {
    const note = document.createElement("p");
    note.textContent = "Search returned nothing. Check login, the dashboard counts, and the valid metadata values, then run Investigate again.";
    resultsEl.appendChild(note);
    return;
  }
  const groups = [];
  const index = new Map();
  for (const hit of hits) {
    const key = (hit.camera_id || "") + "||" + (hit.location || "");
    if (!index.has(key)) {
      const group = { camera_id: hit.camera_id, location: hit.location, hits: [] };
      index.set(key, group);
      groups.push(group);
    }
    index.get(key).hits.push(hit);
  }
  for (const group of groups) {
    const section = document.createElement("section");
    const title = document.createElement("h3");
    title.className = "group-title";
    title.textContent = (group.location || "Unknown location") + " · " + (group.camera_id || "camera");
    section.appendChild(title);
    for (const hit of group.hits) section.appendChild(renderCard(hit));
    resultsEl.appendChild(section);
  }
}

async function investigate() {
  const token = ++pollToken;
  hideError();
  emptyEl.hidden = true;
  clearNode(resultsEl);
  clearNode(stepsEl);
  renderSkipped({});
  investigateBtn.disabled = true;
  try {
    const response = await fetch(apiUrl("api/investigate"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: queryText() }),
    });
    const body = await response.json();
    if (!response.ok) {
      showError(body.error, body.hint);
      return;
    }
    while (token === pollToken) {
      const jobResponse = await fetch(apiUrl("api/jobs/" + body.job_id));
      const job = await jobResponse.json();
      if (!jobResponse.ok) {
        showError(job.error, job.hint);
        return;
      }
      renderSteps(job.steps || []);
      renderSkipped(job);
      if (job.status === "succeeded") {
        renderHits(job.hits || []);
        await loadStatus();
        return;
      }
      if (job.status === "failed") {
        showError(job.error, job.hint);
        return;
      }
      await sleep(1000);
    }
  } catch (_err) {
    showError("The investigation request failed.", "Check that the server is running and run Investigate again.");
  } finally {
    if (token === pollToken) investigateBtn.disabled = false;
  }
}

investigateBtn.addEventListener("click", () => {
  investigate();
});

queryEl.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    investigate();
  }
});

loadStatus();
