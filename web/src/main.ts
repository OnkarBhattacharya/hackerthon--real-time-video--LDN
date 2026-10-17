import "./style.css";
import {
  askQuestion,
  fetchStatus,
  ingestFile,
  localizeStatus,
  startLocalize,
  type StatusResponse,
} from "./api";

const app = document.querySelector<HTMLDivElement>("#app")!;

app.innerHTML = `
  <div class="shell">
    <header class="hero">
      <p class="eyebrow">LDN Hackathon · Real-time video</p>
      <h1>Index, ask, and localize moments</h1>
      <p class="lede">
        Upload a clip, run FFmpeg chunking + YOLO detections, then query an agent or dub a moment to German with compliance flags.
      </p>
    </header>

    <section class="panel">
      <h2>1 · Ingest</h2>
      <div class="row">
        <input type="file" id="file" accept="video/*" />
        <input type="text" id="vidOverride" placeholder="Optional video id" />
        <button id="ingestBtn" type="button">Start ingest</button>
      </div>
      <p id="ingestMsg" class="muted"></p>
      <div class="progress-wrap" hidden>
        <div class="progress-bar"><span id="progressFill"></span></div>
        <p id="statusLine" class="muted"></p>
      </div>
    </section>

    <section class="panel grid-2">
      <div>
        <h2>2 · Ask the agent</h2>
        <textarea id="question" rows="4" placeholder="What happens in the first minute?"></textarea>
        <button id="askBtn" type="button">POST /api/ask</button>
        <pre id="answer" class="code"></pre>
      </div>
      <div>
        <h2>3 · Localize a moment</h2>
        <input type="text" id="momentId" placeholder="moment id (m_…)" />
        <button id="localizeBtn" type="button">POST /api/localize</button>
        <ul id="flags" class="flags"></ul>
        <video id="clip" controls playsinline hidden></video>
        <track id="vttTrack" kind="subtitles" srclang="de" label="German" default />
      </div>
    </section>

    <footer class="muted tiny">API: ingest · status · ask · localize · static /media/*</footer>
  </div>
`;

const fileInput = document.querySelector<HTMLInputElement>("#file")!;
const vidOverride = document.querySelector<HTMLInputElement>("#vidOverride")!;
const ingestBtn = document.querySelector<HTMLButtonElement>("#ingestBtn")!;
const ingestMsg = document.querySelector<HTMLParagraphElement>("#ingestMsg")!;
const progressWrap = document.querySelector<HTMLDivElement>(".progress-wrap")!;
const progressFill = document.querySelector<HTMLSpanElement>("#progressFill")!;
const statusLine = document.querySelector<HTMLParagraphElement>("#statusLine")!;
const question = document.querySelector<HTMLTextAreaElement>("#question")!;
const askBtn = document.querySelector<HTMLButtonElement>("#askBtn")!;
const answer = document.querySelector<HTMLPreElement>("#answer")!;
const momentId = document.querySelector<HTMLInputElement>("#momentId")!;
const localizeBtn = document.querySelector<HTMLButtonElement>("#localizeBtn")!;
const flags = document.querySelector<HTMLUListElement>("#flags")!;
const clip = document.querySelector<HTMLVideoElement>("#clip")!;

let activeVid: string | null = null;
let pollTimer: number | undefined;

function setStatus(s: StatusResponse) {
  progressWrap.hidden = false;
  progressFill.style.width = `${Math.round(s.progress * 100)}%`;
  statusLine.textContent = `${s.state} · ${s.message} · moments ${s.moment_count} · detections ${s.detection_count}`;
  if (s.error) statusLine.textContent += ` · error: ${s.error}`;
}

function startPolling(vid: string) {
  activeVid = vid;
  vidOverride.value = vid;
  if (pollTimer) window.clearInterval(pollTimer);
  const tick = async () => {
    try {
      const s = await fetchStatus(vid);
      setStatus(s);
      if (s.state === "completed" || s.state === "failed") {
        window.clearInterval(pollTimer);
      }
    } catch (e) {
      statusLine.textContent = String(e);
    }
  };
  void tick();
  pollTimer = window.setInterval(tick, 1500);
}

ingestBtn.addEventListener("click", async () => {
  const file = fileInput.files?.[0];
  if (!file) {
    ingestMsg.textContent = "Choose a video file first.";
    return;
  }
  ingestBtn.disabled = true;
  ingestMsg.textContent = "Uploading…";
  try {
    const { vid } = await ingestFile(file, vidOverride.value || undefined);
    ingestMsg.textContent = `Ingest started for ${vid}`;
    startPolling(vid);
  } catch (e) {
    ingestMsg.textContent = String(e);
  } finally {
    ingestBtn.disabled = false;
  }
});

askBtn.addEventListener("click", async () => {
  if (!activeVid) {
    answer.textContent = "Run ingest first (or set a video id).";
    return;
  }
  askBtn.disabled = true;
  answer.textContent = "Thinking…";
  try {
    const res = await askQuestion(activeVid, question.value.trim());
    answer.textContent = `${res.answer}\n\n--- tool trace ---\n${JSON.stringify(res.tool_trace, null, 2)}`;
  } catch (e) {
    answer.textContent = String(e);
  } finally {
    askBtn.disabled = false;
  }
});

localizeBtn.addEventListener("click", async () => {
  if (!activeVid) {
    flags.innerHTML = "<li>Set a video id via ingest first.</li>";
    return;
  }
  const mid = momentId.value.trim();
  if (!mid) {
    flags.innerHTML = "<li>Enter a moment id from the agent search.</li>";
    return;
  }
  localizeBtn.disabled = true;
  flags.innerHTML = "<li>Localizing…</li>";
  try {
    await startLocalize(activeVid, mid);
    const poll = window.setInterval(async () => {
      const job = await localizeStatus(activeVid!, mid);
      if (job.state === "running") return;
      window.clearInterval(poll);
      flags.innerHTML = job.compliance_flags.length
        ? job.compliance_flags.map((f) => `<li>${f}</li>`).join("")
        : "<li>No compliance flags</li>";
      if (job.clip_url) {
        clip.hidden = false;
        clip.src = job.clip_url;
        if (job.vtt_url) {
          const track = clip.querySelector("track");
          if (track) track.src = job.vtt_url;
        }
        void clip.load();
      }
      localizeBtn.disabled = false;
    }, 1200);
  } catch (e) {
    flags.innerHTML = `<li>${e}</li>`;
    localizeBtn.disabled = false;
  }
});
