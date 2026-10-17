# Stage 0 notes

This workspace is not the team VM. The checks below were made here on 17 Oct 2026. Live retrieval, dashboard counts, metadata values, and a W&B chat call were not run, because the variables they need are unset.

## Skills

`.cursor/skills/` is not in this clone. The retrieval paths used by the API are the ones already implemented in `tools/video-ask/vss_client.py`:

| Call | Method and path | Needs | Returns |
| --- | --- | --- | --- |
| Login | `POST /api/v1/auth/login` | JSON `username`, `password` | `access_token` |
| Search | `POST /api/v1/agent/search-and-answer` | Bearer token. Body includes `query`, `top_k`, `metadata_filters.camera_id` | `evidence.chunks[]` with description, score, times, camera, preview source |
| Explore | `GET /api/v1/videos/explore` | Bearer token | `chunks`, `total`, locations |
| Stream | `GET /api/v1/videos/stream?source=&token=` | `s3://` source and the access token | video bytes |

There is no verified segment-detail or neighbour endpoint in that client. Segment fields are read from the search chunk. Neighbours are the previous and next chunks of the same video on the explore page. If they are absent, the hit records `missing_neighbours`.

Search chunk fields used when present: `segment_id` or `id` or `chunk_id`, `video_id` or `original_video` or `filename`, `camera_id`, `location`, `start` / `best_match_start_sec`, `end` / `best_match_end_sec`, `reasoning_content` or `description`, `similarity_score`, `detections`, `preview_source`.

## Environment names

Missing in this shell: `INGRESS_URL`, `VSS_URL`, `VSS_USERNAME`, `VSS_PASSWORD`, `USERNAME`, `PASSWORD`, `WANDB_API_KEY`, `WANDB_BASE_URL`, `WANDB_MODEL`, `COSMOS3_REASON_URL`, `YOLO_URL`, `COSMOS_EMBED1_URL`, `CANARY_1B_URL`, `TICKET_WEBHOOK_URL`.

## Decisions

- Crop: unavailable. No frame or bounding-box call was verified.
- Audio: out of scope. Speech was not checked on a real clip.
- Ranking and German reports use templates until `WANDB_API_KEY` is set. The UI says when a score did not come from the model.
- `VSS_USE_FIXTURE=1` serves labelled sample segments so the API and page can be exercised without the archive. That is not indexed footage.
