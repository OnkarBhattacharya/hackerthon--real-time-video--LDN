const questionEl = document.getElementById("question");
const askBtn = document.getElementById("askBtn");
const statusEl = document.getElementById("status");
const answerEl = document.getElementById("answer");
const clipsEl = document.getElementById("clips");
const chipsEl = document.getElementById("chips");
const exploreEl = document.getElementById("explore");

/** Prefix for Ingress path /app (page may be /app or /app/). */
function apiUrl(path) {
  const base = window.location.pathname.replace(/\/?$/, "/");
  return base + String(path || "").replace(/^\//, "");
}

function fmtScore(n) {
  if (typeof n !== "number") return "—";
  return n.toFixed(2);
}

function fmtRange(start, end) {
  const a = typeof start === "number" ? `${start}s` : "?";
  const b = typeof end === "number" ? `${end}s` : "?";
  return `${a}–${b}`;
}

function renderClips(clips) {
  clipsEl.innerHTML = "";
  if (!clips || !clips.length) {
    const p = document.createElement("p");
    p.className = "card-note";
    p.textContent = "No evidence clips returned for this question.";
    clipsEl.appendChild(p);
    return;
  }
  for (const clip of clips) {
    const wrap = document.createElement("div");
    wrap.className = "clip";
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.innerHTML = `<strong>${fmtScore(clip.similarity_score)}</strong>
      <span>${fmtRange(clip.start_sec, clip.end_sec)}</span>
      <span>${clip.filename || "segment"}</span>`;
    wrap.appendChild(meta);
    if (clip.stream_path) {
      const video = document.createElement("video");
      video.controls = true;
      video.playsInline = true;
      video.preload = "metadata";
      video.src = apiUrl(clip.stream_path);
      wrap.appendChild(video);
    }
    clipsEl.appendChild(wrap);
  }
}

async function ask(question) {
  const q = (question || "").trim();
  if (!q) return;
  questionEl.value = q;
  askBtn.disabled = true;
  statusEl.textContent = "Searching the index…";
  answerEl.textContent = "";
  clipsEl.innerHTML = "";
  try {
    const res = await fetch(apiUrl("api/ask"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: q, top_k: 8, min_similarity: 0.25 }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    answerEl.textContent = data.answer || "(empty answer)";
    renderClips(data.clips || []);
    const hits = data.segment_hit_count != null ? ` · ${data.segment_hit_count} segment hits` : "";
    statusEl.textContent = `Done${hits}`;
  } catch (err) {
    statusEl.textContent = String(err.message || err);
    answerEl.textContent = "Request failed.";
  } finally {
    askBtn.disabled = false;
  }
}

async function loadSuggestions() {
  try {
    const res = await fetch(apiUrl("api/suggestions"));
    const data = await res.json();
    const prompts = data.search_prompts || [];
    chipsEl.innerHTML = "";
    for (const p of prompts.slice(0, 8)) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "chip";
      btn.textContent = p;
      btn.addEventListener("click", () => ask(p));
      chipsEl.appendChild(btn);
    }
  } catch {
    chipsEl.innerHTML = "";
  }
}

async function loadExplore() {
  try {
    const res = await fetch(apiUrl("api/explore?limit=6"));
    const data = await res.json();
    exploreEl.innerHTML = "";
    const chunks = data.chunks || [];
    if (!chunks.length) {
      exploreEl.innerHTML = `<p class="card-note">No indexed chunks yet.</p>`;
      return;
    }
    for (const c of chunks) {
      const card = document.createElement("div");
      card.className = "clip";
      const meta = document.createElement("div");
      meta.className = "meta";
      meta.innerHTML = `<span>${c.location || "unknown location"}</span>
        <span>${c.camera_id || "cam?"}</span>
        <span>${fmtRange(c.start_sec, c.end_sec)}</span>`;
      const note = document.createElement("p");
      note.className = "card-note";
      note.textContent = c.reasoning_content || c.filename || "";
      card.appendChild(meta);
      card.appendChild(note);
      if (c.stream_path) {
        const video = document.createElement("video");
        video.controls = true;
        video.playsInline = true;
        video.preload = "none";
        video.src = apiUrl(c.stream_path);
        card.appendChild(video);
      }
      const askThis = document.createElement("button");
      askThis.type = "button";
      askThis.className = "chip";
      askThis.textContent = "Ask about scenes like this";
      askThis.addEventListener("click", () => {
        const seed = (c.reasoning_content || "").split(/[.!?]/)[0] || c.filename;
        ask(seed);
      });
      card.appendChild(askThis);
      exploreEl.appendChild(card);
    }
  } catch (err) {
    exploreEl.innerHTML = `<p class="card-note">${String(err.message || err)}</p>`;
  }
}

askBtn.addEventListener("click", () => ask(questionEl.value));
questionEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
    e.preventDefault();
    ask(questionEl.value);
  }
});

loadSuggestions();
loadExplore();
