# Stage 6 — Front end

**Time box:** 45 minutes
**Depends on:** Stage 5 endpoints on localhost
**Blocks:** deploy. Fix broken UI flows before Stage 7.

Shared rules: [constraints.md](constraints.md).

## Outcome

One page, served by FastAPI at `/`, lets an operator run an investigation and review the ranked evidence without a build tool or a network dependency.

## Files

- `frontend/index.html`
- `frontend/app.js`
- `frontend/styles.css`

No framework, no bundler, no external CDN, no remote fonts. System font stack only. All API calls use relative URLs (`api/investigate`, not a hard-coded host or `/app` prefix).

## Page

**Header**

- Title.
- One sentence: the tool searches the indexed archive for close approaches between people and moving vehicles, ranks them, and shows the footage.
- Status line: indexed segment count when the health or last-run payload includes it, and the last run time from `generated_at` when a snapshot exists. If those values are not loaded yet, the line says the archive has not been queried in this session.

**Investigate**

- One primary button labelled `Investigate`.
- An optional text box. Placeholder and default value: `person close to a moving vehicle`. Empty input sends the default.
- While a job is running, the button is disabled and the step list is live.

**Live steps**

Poll `GET api/jobs/{job_id}` about once a second. Render `steps` in order. Stop polling on `succeeded` or `failed`. Show the error and hint from a failed job in the error state, not only in the console.

**Results**

Group hits by camera and location. Inside each group, keep the API order (severity, then confidence, then search score).

Each card shows:

- a severity badge with both colour and text (`1` through `5` plus a word: none, distant, close, evasive, contact). Colour is never the only signal.
- the one-line rationale
- camera id and the time window
- the triptych: three small `<video controls>` players labelled `Before`, `Event`, and `After`
- the event player visually highlighted
- a labelled note instead of a player when that neighbour is in `missing_neighbours`
- the object crop when `crop_url` is present
- privacy flags and the GDPR note in a visible amber region, not behind a disclosure widget

**Card actions**

- `Report (English)` → `POST api/hits/{segment_id}/report?lang=en`
- `Bericht (Deutsch)` → `POST api/hits/{segment_id}/report?lang=de`
- `Create ticket` → `POST api/hits/{segment_id}/ticket`

Show the returned report or ticket in a panel on that card. The panel has:

- `Copy`, which writes the text to the clipboard and confirms it
- `Download`, which saves the report as `.txt` or the ticket as `.json`

If German was scope-cut, the Deutsch button remains visible and shows the API error and hint. Do not hide the failure.

## States

- **Empty:** no investigation yet. Say what Investigate will do.
- **No hits:** the job succeeded and `hits` is empty. Say that search returned nothing and that login, dashboard counts, and metadata values should be checked.
- **Skipped camera:** if the job steps or payload name a camera that is not indexed, show that camera as skipped.
- **Error:** show `error` and `hint`. Say that the operator can run Investigate again.
- **Fallback ranking:** if a hit is marked rule-based, the badge caption says the score did not come from the model.

## Design

- High contrast text on the background.
- Usable at a laptop width and at a phone width. Cards stack. Videos do not overflow the viewport.
- Every button and the text box has a visible keyboard focus style.
- Hit targets are large enough to tap.
- Do not autoplay video with sound.

## Verification

Run the app and click through once as an operator:

1. Load `/` and read the empty state.
2. Run Investigate with the default query.
3. Watch the step list update until results appear.
4. Confirm groups, severity badges, rationale, and the event clip. Play Before and After where present. Confirm a missing neighbour is labelled.
5. Open the English report, copy it, and download it.
6. Open the German report.
7. Create a ticket and download the JSON.
8. Narrow the window to a phone width and confirm the card is still readable and the buttons are reachable.
9. Reload the page and confirm the status line can show the last run time from the snapshot.

Write anything broken in `PROGRESS.md` and fix it before deployment. A screenshot of the loaded page is not this verification.

## Done when

- The click-through works on localhost.
- `PROGRESS.md` lists what was exercised and what was fixed.
