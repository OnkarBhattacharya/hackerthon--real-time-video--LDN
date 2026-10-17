# Hackerthon — Real-Time Video (LDN)

**Repository:** [github.com/ahmedm87/hackerthon--real-time-video--LDN](https://github.com/ahmedm87/hackerthon--real-time-video--LDN)

```bash
git clone https://github.com/ahmedm87/hackerthon--real-time-video--LDN.git
cd hackerthon--real-time-video--LDN
```

Browser UI (**Vite + TypeScript → `web/dist/`**, served by FastAPI) over a video ingest, indexing, agent, and localization pipeline.

## Architecture

```
Browser (Vite TS → dist/)
  POST /api/ask, /api/localize, /api/ingest
  GET  /api/status/{vid}, /media/*
        ↓
FastAPI (app/main.py)
├── IngestService (ingest.py, BackgroundTasks)
│     → FfmpegService (ffmpeg.py)
│     → ModelClient (models.py)
│     → YoloService (yolo.py)
│     → MomentStore (store.py → data/<vid>/index.json)
├── AgentService (agent.py)
│     → tools: search_moments, query_detections, ask_vision, localize_moment
└── LocalizeService (localize.py)
      → clip + ASR + translate + VTT (+ optional TTS dub)
```

Processed assets live under `data/<vid>/` (`chunks/`, `frames/`, `dubs/`, `index.json`) and are exposed read-only at `/media`.

## Requirements

- Python 3.11+
- Node 20+
- `ffmpeg` / `ffprobe` on `PATH`

Optional (see `requirements-ml.txt`):

- `ultralytics` — in-process YOLO
- `faster-whisper` — ASR
- `edge-tts` — German TTS dubs

## Setup

```bash
python3 -m venv .venv   # on Debian: sudo apt install python3-venv
source .venv/bin/activate
pip install -r requirements.txt
# optional ML stack:
# pip install -r requirements-ml.txt

cd web && npm install && npm run build && cd ..
```

Copy `.env.example` to `.env` and add keys when you want live models (mock fallbacks work without keys).

## Run

**Production-style (static UI + API on one port):**

```bash
source .venv/bin/activate
./scripts/dev.sh
# or: uvicorn app.main:app --host 0.0.0.0 --port 8765
```

Open http://127.0.0.1:8765

**Frontend hot reload (proxied to API):**

```bash
# terminal 1
uvicorn app.main:app --port 8765 --reload
# terminal 2
cd web && npm run dev   # http://127.0.0.1:4317
```

## API

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/ingest` | Multipart `file` and/or form `source_url`, optional `vid` |
| GET | `/api/status/{vid}` | Ingest progress + index counts |
| POST | `/api/ask` | JSON `{ "vid", "question" }` — agent tool loop |
| POST | `/api/localize` | JSON `{ "vid", "moment_id", "target_lang" }` |
| GET | `/api/localize/{vid}/{moment_id}` | Localization job status |
| GET | `/media/{vid}/...` | Static processed media |

## Environment

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_API_KEY` | Vision captions, LLM agent, translation |
| `OPENAI_API_KEY` | Text embeddings for moment search |
| `DATA_DIR` | Default `data/` |
| `ENABLE_YOLO` | `true`/`false` (default true) |
| `ENABLE_WHISPER` | ASR for localization |
| `ENABLE_TTS` | Edge TTS dub track |
