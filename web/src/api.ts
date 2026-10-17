export type JobState = "pending" | "running" | "completed" | "failed";

export interface StatusResponse {
  vid: string;
  state: JobState;
  progress: number;
  message: string;
  moment_count: number;
  detection_count: number;
  index_path: string | null;
  error: string | null;
}

export interface AskResponse {
  vid: string;
  answer: string;
  tool_trace: Record<string, unknown>[];
}

export interface LocalizeResponse {
  vid: string;
  moment_id: string;
  state: JobState;
  clip_url: string | null;
  vtt_url: string | null;
  dub_url: string | null;
  compliance_flags: string[];
  message: string;
}

export async function ingestFile(file: File, vid?: string): Promise<{ vid: string }> {
  const form = new FormData();
  form.append("file", file);
  if (vid) form.append("vid", vid);
  const res = await fetch("/api/ingest", { method: "POST", body: form });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function fetchStatus(vid: string): Promise<StatusResponse> {
  const res = await fetch(`/api/status/${encodeURIComponent(vid)}`);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function askQuestion(vid: string, question: string): Promise<AskResponse> {
  const res = await fetch("/api/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ vid, question }),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function startLocalize(
  vid: string,
  momentId: string,
  targetLang = "de-DE",
): Promise<LocalizeResponse> {
  const res = await fetch("/api/localize", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      vid,
      moment_id: momentId,
      target_lang: targetLang,
      include_subtitles: true,
      include_dub: false,
    }),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function localizeStatus(
  vid: string,
  momentId: string,
): Promise<LocalizeResponse> {
  const res = await fetch(
    `/api/localize/${encodeURIComponent(vid)}/${encodeURIComponent(momentId)}`,
  );
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}
