# Stage 7 — Deploy and submit

**Time box:** 45 minutes
**Depends on:** Stage 6 click-through on localhost, and the acceptance list in [constraints.md](constraints.md)
**Blocks:** nothing. This is the last stage.

## Outcome

The app is running at ingress path `/app` on the team host, one investigation has been run there, and `README.md` plus `SUBMISSION.md` describe the system that actually shipped.

## Deploy

Read `.cursor/skills/` `deployment/deploy-app-no-registry` in full and follow it exactly.

Required shape:

- Kubernetes
- the operator's team namespace
- no Docker build and no registry push
- Ingress path `/app` on the team host
- the Stage 5 router, so the prefix rewrite still serves `/api` and `/`

Use the skill's Deployment, Service, and Ingress. Config and secrets come from the environment names recorded in Stage 0. The in-cluster VSS address is preferred when the skill says the pod cannot resolve the public ingress DNS. Do not commit the kubeconfig, the password, or the rendered secret.

After the rollout is ready:

1. Open `$INGRESS_URL/app`.
2. Run one full Investigate on that deployed page.
3. Confirm the event clip plays in the browser. Confirm a present neighbour plays. Confirm English report, German report, and ticket download.

If deploy cannot be made ready inside the time box, keep the app running locally on the VM, say that in `README.md` and `SUBMISSION.md`, and do not claim the ingress URL serves this build.

## README

Replace the current video-ask README with the investigation app. Keep it accurate. Required contents:

- what it does
- how it works
- an architecture sketch of about eight lines, as a text diagram (the diagram in [constraints.md](constraints.md) is the starting point; adjust it to the code that shipped)
- how to run locally
- how it is deployed (`/app`, no image build)
- limits and known gaps, including every fallback and scope cut that was used

## Submission

Run the submission skill (`help me submit our project`). It writes `SUBMISSION.md`. Before pushing, check that the file states:

- the problem: finding and explaining close approaches between people and moving vehicles in the indexed archive
- the data used: which packs and cameras actually returned hits
- how search is used and how the LLM is used
- what is real versus stubbed or fallback (ranking, crops, audio, German, webhook)
- the repository link

## Push

Commit only when the operator asks. When they do, push the repo and return the URL they can paste into the submission form.

Do not commit `.env`, kubeconfig, `data/last_run.json`, or `samples/` if the saved search response contains tokens or signed URLs that grant access. If a sample must stay, strip secrets first.

## Verification

- Deployed `/app` investigation completed, or the local-VM fallback is explicitly documented.
- Clips play on the surface that will be demonstrated.
- `README.md`, `NOTES.md`, `PROGRESS.md`, and `SUBMISSION.md` agree with each other.
- `pytest` passes.
- `git status` shows no secret files staged.

## Done when

- The operator has the repository link.
- The acceptance checklist in [constraints.md](constraints.md) is checked against the running app, and any open item is written as a gap in `SUBMISSION.md` rather than left implied.
