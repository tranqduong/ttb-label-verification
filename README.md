# TTB Label Verification — Prototype

An AI-assisted prototype that checks whether an alcohol beverage label's
artwork matches the data filed in its COLA application: brand name,
class/type, ABV, net contents, bottler info, country of origin (imports),
and the mandatory government health warning statement. Single Label Review
submissions are persisted to an **Application Queue** — a reviewer can come
back later, filter by status, and their Approve/Flag/Reject decision (with
note) is saved, not lost on refresh.

Built for the take-home brief based on discovery interviews with TTB's
Compliance Division. This is a standalone proof-of-concept — it does **not**
integrate with the live COLA system (that integration was explicitly out of
scope per IT).

## How it works

1. An agent uploads a photo of the label and the data filed in the
   application (brand name, class/type, ABV, net contents, etc). This save
   step (`POST /api/applications`) only writes the image + filed data to
   the Application Queue as `pending_analysis` — it does **not** call the
   vision model yet.
2. Analysis is a separate, explicit step: opening the submission (or its
   Application Queue record later) shows the label image on file and an
   "Analyze Label" button. Clicking it (`POST /api/applications/{id}
   /analyze`) is what sends the image to a vision-capable LLM (Claude) with
   a prompt that extracts the label's fields verbatim, including
   formatting cues for the government warning (all-caps? bold?), and is
   instructed to mark a field "UNREADABLE" (illegible, re-photograph might
   help) rather than guess, distinct from a field that's simply absent
   from the label. Splitting submission from analysis this way — rather
   than running both in one request — means a reviewer can browse a
   backlog of saved label photos without every single one having paid for
   a vision-extraction call just to land in the queue.
3. A pure-Python comparison layer checks each field against the filed data
   and returns a three-tier verdict per field — **pass** / **needs
   review** / **fail** — with a plain-English reason. "Needs review" is
   deliberate, not a hedge: some real label discrepancies are a judgment
   call (a related-but-not-identical name, a likely misread accent, an
   illegible field), and forcing those into a binary pass/fail either
   creates false rejections or silently swallows things a human should
   actually look at.
4. Once analysis runs, results render as Expected (filed) vs. Detected (on
   label) field cards, each with a reviewer-facing badge (Match / Minor
   Variance / On Label / Needs Review / Unreadable / Mismatch — see
   `badges.py`) — readable at a glance, no hunting for buttons — with
   Approve / Reject / Flag actions an agent can record against the result
   (Reject and Flag require a short comment, per the brief's spec). The
   submission, its analysis result, and the decision are all persisted to
   the Application Queue (Postgres), so a reviewer can leave and come back
   to it later.
5. A batch mode accepts multiple label photos + a matching array of
   application data and processes them concurrently, analyzing
   immediately (there's no reviewer-facing queue to browse first in batch
   mode). Batch stays stateless by design — see "Application Queue scope"
   below.

## Why these specific design decisions

These map directly to things stakeholders said in discovery, not generic
best practices:

- **Three-tier fuzzy matching for brand name / class-type / bottler info /
  country of origin, but zero-tolerance exact matching for the government
  warning.** Dave (28-year agent) pointed out that "STONE'S THROW" vs
  "Stone's Throw" is obviously the same brand and shouldn't be flagged —
  that requires judgment, not string equality. Jenny (newer agent) pointed
  out the opposite for the warning statement: TTB actually rejects labels
  for using "Government Warning" in title case instead of "GOVERNMENT
  WARNING" in all caps, or for bold formatting being dropped. So the
  warning check (`_check_government_warning` in `verification.py`) is
  intentionally strict on both wording and the two formatting cues, while
  the other text fields (`_fuzzy_field`) run through several normalization
  tiers before landing on a verdict:
  - exact / case-insensitive match → **pass**
  - same once accented characters are stripped (e.g. "La Rojeña" vs "La
    Rojenia") → **needs review**, flagged as a likely misread diacritic
    rather than a real name difference
  - one value is a truncated/expanded form of the other (e.g. "Sierra
    Nevada" vs "Sierra Nevada Brewing Co.") → **pass** — a dropped/added
    corporate suffix isn't a different identity
  - high similarity score or meaningful shared wording (e.g. "Korbel Brut"
    vs "Korbel Champagne Cellars") → **needs review** — related but not
    clearly identical, worth a reviewer's glance rather than an automatic
    hard fail
  - otherwise → **fail**
  - a field the vision model marks `UNREADABLE` (glare/angle/blur
    prevented a confident read) → **needs review**, distinct from a field
    that's genuinely not printed on the label at all (**fail**) — the
    former is fixed by a re-photograph, the latter isn't
- **ABV and net contents get unit-aware comparison, not just fuzzy text
  matching.** A label stating strength as "90 Proof" and an application
  filed as "45% Alc./Vol." are the same alcohol content (proof = 2× ABV%);
  a label reading "70 CL" against an application's "700 mL" is the same
  volume. Both are parsed to a common unit and compared numerically before
  falling back to text similarity, and a real numeric disagreement (e.g.
  13.0% filed vs. 14.5% on the label) is treated as authoritative — an ABV
  mismatch is a genuine compliance issue, never softened into "just a
  wording variance" the way a close text-similarity score might suggest.
  An EU "estimated fill" mark (a trailing "e"/"℮" after the quantity, e.g.
  "750 mL e") is recognized as fill-quantity boilerplate and stripped
  before comparing, not treated as a different unit.
- **"Minor Variance" is restricted to identity fields — never to alcohol
  content, net contents, or the government warning.** `verification.py`'s
  own pass/fail logic already treats a unit-converted match (e.g. "80
  Proof" on the label against a filed "40%") as a clean pass — that part
  hasn't changed. What changed is the *presentation* layer (`badges.py`):
  those regulated/quantitative fields now always render as a plain "Match"
  or a hard "Mismatch," never a softened "Minor Variance," even though the
  underlying pass reason text happens to mention a unit conversion. Only
  brand name, bottler info/address, class/type, and country of origin —
  free-text identity fields — can show "Minor Variance" on a near-exact
  pass (Dave's casing/punctuation tolerance). This was corroborated against
  a second reference implementation of this same brief, whose README makes
  the identical distinction explicit: free-text fields tolerate
  formatting differences, but "the Government Warning is held to a
  stricter, closer-to-verbatim check than other free-text fields... it's
  statutory language, not a paraphrasable field" — and the same logic
  extends naturally to ABV and net contents, which are regulated numbers,
  not prose.
- **A vision LLM instead of OCR + regex.** Labels vary enormously in layout
  and font, and the warning-statement formatting check specifically needs
  visual cues (bold, capitalization) that plain OCR text loses. An LLM with
  a tightly-scoped extraction prompt handles that variance without a
  hand-tuned parser per label style.
- **Batch upload with concurrent processing.** Sarah described importers
  dumping 200-300 applications at once, currently processed one at a time.
  `/api/verify/batch` accepts multiple images + a matching JSON array and
  processes them concurrently (capped at 6 in-flight extraction calls to
  avoid slamming the vision API).
- **Speed as an explicit design constraint, not an afterthought.** The
  scanning-vendor pilot failed because 30-40 seconds per label was
  unusable — agents went back to eye-checking because it was faster. A
  single Claude vision call typically returns in a few seconds; this is
  called out explicitly as a trade-off below rather than glossed over,
  since it's still slower than the 5-second bar Sarah set.
- **Deliberately plain UI.** Sarah's calibration point was her 73-year-old
  mother. No dashboards, no configuration screens — upload, fill in a form,
  get a color-coded verdict per field with a one-line reason. Both upload
  screens use a drag-and-drop zone (click-to-browse still works) rather
  than a bare file input, and the batch results table has a search box,
  a verdict filter, and a CSV export — small additions, but they're the
  difference between "here's some JSON" and something an agent doing
  triage on 300 files can actually use. The batch progress bar reflects
  real per-item completions streamed from the backend as they finish
  (`/api/verify/batch/stream`), not a decorative animation timed to guess
  how long the batch might take.
- **Approve / Reject / Flag actions, with a required comment on the
  latter two, persisted to the Application Queue.** Straight from the
  brief's screen spec: an agent needs to act on the tool's verdict, not
  just read it, and a rejection or escalation should carry a reason for
  whoever looks at it next. `PATCH /api/applications/{id}` writes the
  decision + note to Postgres, so it survives a page refresh or a
  reviewer coming back to it the next day — this is an upgrade from an
  earlier build of this prototype that only recorded the decision
  client-side for the current session (see "Application Queue scope"
  below for what is and isn't persisted).
- **Anti-hallucination rules baked into the extraction prompt, not left
  to hope.** Two concrete, verified failure modes shaped this: a vision
  model asked to read a recognized brand's ABV will sometimes recall a
  *typical* proof/ABV from training data instead of reading the actual
  bottle, and will sometimes report a "Serving size" figure as the net
  contents instead of the container's total volume. Both are confident-
  sounding and wrong — exactly the kind of silent error this tool exists
  to catch, not commit. The prompt explicitly instructs the model to
  transcribe only what's visible and prefer "UNREADABLE" over a guess.
- **No COLA integration.** Per IT (Marcus), this is a standalone
  proof-of-concept; COLA integration has its own authorization
  requirements and is explicitly a future-procurement question, not part
  of this prototype.

### Application Queue scope

Only **Single Label Review** submissions are queued, and queuing is split
into two steps: `POST /api/applications` writes the application data and
the label image to Postgres as `pending_analysis` (no extraction yet),
then `POST /api/applications/{id}/analyze` runs extraction against that
stored image and fills in the field results, moving the record to
`needs_review`. The Approve/Flag/Reject decision + note persist against
that same record once analysis has run.
**Batch Upload stays stateless**, deliberately: it still calls the
original `/api/verify/batch` / `/api/verify/batch/stream` endpoints and
returns results without saving them. Queuing 200-300 batch items would
need its own pagination/bulk-action UI that's out of scope for this pass,
and batch's existing CSV export already covers "get all these results
out" for the importer-dump scenario Sarah described. This does mean the
PII/retention question Marcus raised is now live for queued submissions
(unlike the fully-stateless earlier build) — see "Known limitations."

## Stack

- **Backend:** Python, FastAPI, `anthropic` SDK for the vision call,
  `rapidfuzz` for similarity scoring, Pydantic for schemas, `asyncpg` for
  the Application Queue's Postgres persistence.
- **Frontend:** a single static HTML/CSS/vanilla-JS page (no build step,
  no framework) served directly by FastAPI. Chosen over a React/Vite setup
  because the deliverable is a small prototype, not a maintained product —
  and it means "clone and run" has one moving part, not two.
- **Persistence:** Postgres, accessed directly via `asyncpg` (no ORM) in
  `app/services/db.py` — only the Application Queue (Single Label Review
  submissions + reviewer decisions) is persisted; see "Application Queue
  scope" above. Locally this needs a `DATABASE_URL` (or `POSTGRES_URL`) env
  var pointing at any Postgres instance; on Vercel it's provisioned via the
  project's Storage tab (see Deployment below).
- **Vision model:** Claude (`claude-haiku-4-5` by default, overridable via
  `ANTHROPIC_VISION_MODEL`). This is a verbatim-transcription task, not one
  needing deep reasoning, so a smaller/faster model is the right fit for
  Sarah's "5 seconds or nobody uses it" bar — a larger model buys accuracy
  this task mostly doesn't need at the cost of the one budget stakeholders
  actually care about. The extraction call is isolated in
  `app/services/extraction.py` behind a single function, so switching
  models (or providers — GPT-4o, Gemini) only touches that file.

## Setup & run

### Prerequisites
- Python 3.10+
- An Anthropic API key ([console.anthropic.com](https://console.anthropic.com))

### Install & run

```bash
cd backend
pip install -r requirements.txt
cp .env.example .env
# edit .env and paste in your ANTHROPIC_API_KEY

uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Then open **http://localhost:8000** — the frontend is served directly by
the backend, so there's nothing separate to start.

### Running the tests

The comparison logic (fuzzy matching, warning-statement strictness, ABV/net
contents normalization) is unit-tested without touching the network — no
API key required:

```bash
pip install pytest  # if not already installed
python3 -m pytest tests/test_verification.py -v
```

### Trying it out with sample labels

`sample_labels/` contains four synthetically-generated label images (via
`generate_samples.py`, using Pillow — no AI image generation dependency, so
they regenerate deterministically) plus `sample_applications.json` with the
matching filed data for each:

| File | Scenario | Expected result |
|---|---|---|
| `clean_match.jpg` | Everything matches | PASS |
| `casing_mismatch_ok.jpg` | Brand name differs only in case/punctuation (Dave's example) | PASS |
| `warning_titlecase_fail.jpg` | Government warning in Title Case, not bold (Jenny's example) | FAIL |
| `abv_mismatch_fail.jpg` | Label ABV doesn't match filed ABV | FAIL |

`tests/test_verification.py` covers the tiered-verdict cases (needs-review
tier, proof/ABV and mL/cL/L unit conversion, diacritic misreads, truncated
brand names) directly against the comparison logic, without needing images
or an API key.

In the UI's **Batch Upload** tab, click "Load sample batch data" to
pre-fill matching JSON for `clean_match.jpg` and `casing_mismatch_ok.jpg`,
or use the **Single Label** tab with any one sample image and its
corresponding entry from `sample_applications.json`.

To regenerate or add more sample labels:
```bash
cd sample_labels && python3 generate_samples.py
```

## API

- `POST /api/verify` — multipart form: `label_image` (file) +
  `application_data` (JSON string matching the `ApplicationData` schema).
  Returns per-field results (each `pass` / `needs_review` / `fail` /
  `not_applicable`) and an overall `pass` / `needs_review` / `fail`.
- `POST /api/verify/batch` — multipart form: `label_images` (multiple
  files) + `application_data` (JSON array, same length/order as the
  images). Returns a summary count plus per-item results once the whole
  batch finishes.
- `POST /api/verify/batch/stream` — same inputs, but streams one
  newline-delimited JSON `{"type": "progress", "completed": N, "total": M,
  ...}` line as each item actually finishes (items run concurrently, so
  these arrive out of upload order — the UI shows the real count as it
  happens, not a decorative animation), followed by a final `{"type":
  "done", "result": {...}}` line with the same shape `/verify/batch`
  returns. The frontend's batch tab uses this endpoint for its live
  progress bar.
- `POST /api/applications` — multipart form: `label_image` (file) +
  `application_data` (JSON string). Persists the image + filed data to the
  Application Queue (Postgres) as `pending_analysis` — does **not** run
  extraction. Returns 503 if no Postgres connection string is configured
  (see Deployment).
- `POST /api/applications/{id}/analyze` — runs the same extraction +
  comparison as `POST /api/verify` against the image already on file for
  that application, persists the result, and moves the record to
  `needs_review`. Safe to call again (re-runs extraction and overwrites
  the previous result).
- `GET /api/applications/{id}/image` — serves back the label photo saved
  with a submission, so the Queue detail view can show it before (and
  after) analysis has run.
- `GET /api/applications` — list all queued applications, with per-status
  counts (`all` / `pending_analysis` / `flagged` / `needs_review` /
  `pending` / `approved`) for the Queue tab's filter chips.
- `GET /api/applications/{id}` — full detail for one queued application,
  including per-field results and reviewer-facing badges (empty until
  analysis has run).
- `PATCH /api/applications/{id}` — record a reviewer decision:
  `{"status": "approved" | "flagged" | "pending", "note": "..."}`. `note`
  is required by the frontend (not the API schema) for `flagged`/`pending`.
- `GET /healthz` — liveness check.

Interactive API docs (Swagger UI) are available at `/docs` once the server
is running.

## Deployment

The deployed instance for this take-home runs on **Vercel**: the frontend
is a static file, and `api/index.py` is a single Python serverless function
(`vercel.json` routes `/api/*` to it and raises its `maxDuration` to 60s for
the vision call). Any other platform that runs a Python ASGI app also works
(Render, Railway, Fly.io, etc) — swap step 2 below for that platform's
start-command equivalent.

Minimum steps:

1. Set the `ANTHROPIC_API_KEY` environment variable in the platform's
   secrets/config. `/api/verify` and `/api/verify/batch*` (the stateless
   endpoints) work with just this.
2. On a non-Vercel platform, start command: `uvicorn app.main:app --host
   0.0.0.0 --port $PORT` (from the `backend/` directory). On Vercel this is
   handled by `api/index.py` + `vercel.json` — no start command to set.
3. **For the Application Queue:** provision a Postgres database and set
   `DATABASE_URL` (or `POSTGRES_URL`). On Vercel this is one click — add
   Postgres from the project's **Storage** tab, which auto-injects the
   connection string as an env var, no manual credential entry. Without
   this, `/api/applications/*` returns `503` with a message explaining why;
   `/api/verify` (stateless single-label) and `/api/verify/batch*` are
   unaffected either way, since they never touch the database.

**Network note (per IT feedback):** the failed vendor pilot lost features
because the agency firewall blocked outbound calls to the vendor's ML
endpoints. A production deployment inside TTB's network would need
`api.anthropic.com` (or whichever provider is chosen) added to the
outbound allowlist. This prototype's deployed instance runs outside that
network, so it isn't affected, but it's a real blocker to flag for anyone
evaluating this for internal rollout.

## Known limitations & trade-offs

Documenting these explicitly rather than glossing over them, per the
brief's ask for "a working core application... with documented trade-offs":

- **Latency vs. the 5-second bar.** A single Haiku-tier vision call
  typically returns in a few seconds, which beats the 30-40 second vendor
  pilot that agents rejected, and should be close to Sarah's "5 seconds or
  nobody uses it" bar for a single label — but this hasn't been measured
  with a real API key in this build environment (see the last item below),
  and larger batch runs will take longer in aggregate even with concurrent
  processing. A production version might also tile or upscale very
  large/small source photos before sending them to the model — a technique
  worth calling out even though it's not implemented here — since a phone
  photo downscaled by the model's own image preprocessing can blur out
  fine print (small accented characters in particular) enough to cause
  misreads; that's a meaningful accuracy lever left on the table for time.
- **No handling of poor-quality images beyond flagging them.** Jenny
  flagged photos taken at odd angles, with bad lighting, or glare as a
  real pain point, but called it out-of-scope for a prototype. This
  version reports low overall extraction confidence and a per-field
  "UNREADABLE" signal (routed to "needs review" with a re-photograph
  recommendation) rather than silently guessing, but it doesn't attempt
  image correction (deskewing, glare removal, tiling for resolution, etc).
- **The similarity thresholds (92% pass / 60% needs-review) are
  heuristics, not a TTB-approved rule.** They were chosen to pass Dave's
  real "STONE'S THROW" example and the "Korbel Brut" vs. "Korbel Champagne
  Cellars" judgment-call case while still failing genuinely different
  names, but a real deployment would want compliance staff to tune and
  sign off on these thresholds against a labeled sample set — ideally with
  a small ground-truth evaluation harness (a handful of hand-labeled real
  label photos, replayed on every prompt/threshold change) rather than
  eyeballing individual cases, before trusting it unsupervised.
- **The required government warning text is hardcoded** to the standard
  federal wording (27 CFR 16.21). It doesn't handle any TTB-approved
  alternate phrasings or size/placement rules beyond caps/bold, since the
  brief didn't specify those and confirming the full rule set was out of
  scope for the time box.
- **No authentication or rate limiting.** The Application Queue now
  persists submissions and reviewer decisions (Postgres — see "Application
  Queue scope" above), which is a real change from an earlier build of
  this prototype that discarded everything per-request; but there's still
  no login, no per-agent attribution on a decision, and no rate limiting.
  A production tool handling real applications would need all of these,
  plus the retention-policy conversation Marcus flagged, now that
  application data and label photos' extracted text genuinely persist
  rather than being processed-and-discarded.
- **Label photos are stored as raw bytes in Postgres (`BYTEA`), not
  object storage.** Simplest thing that works for a prototype-scale queue
  (one extra column, no S3/blob bucket to wire up), but it means every
  image round-trips through the database connection on save, analyze, and
  display, and the free-tier database's 0.5 GB cap will fill up faster
  than it would with images in object storage and only a URL in Postgres.
  A production version should move label photos to blob storage (S3,
  Vercel Blob, etc.) and store a reference instead.
- **Batch Upload results are still not persisted, by design.** See
  "Application Queue scope" above for the reasoning (no bulk-action UI in
  scope, CSV export already covers extraction). This means the
  PII/retention question only applies to the Single Label Review /
  Application Queue path, not batch.
- **Single beverage-type schema.** TTB's exact required fields vary by
  beverage type (beer/wine/spirits); this prototype uses one common-
  denominator schema with a `beverage_type` selector for context, rather
  than fully modeling class-specific requirements (e.g. wine's varying ABV
  labeling exceptions).
- **Live end-to-end testing update:** this was smoke-tested against the
  real Anthropic API against all four sample labels, and it surfaced two
  real issues that are now fixed (both are why "test against live data
  before calling it done" matters more than unit tests alone):
  - The pinned `anthropic==0.34.2` in `requirements.txt` was incompatible
    with the `httpx` version it resolved to (`TypeError: Client.__init__()
    got an unexpected keyword argument 'proxies'`) — bumped to
    `anthropic==0.121.0`.
  - The government warning check originally required exact
    punctuation-for-punctuation wording, with zero tolerance. Live runs
    showed the vision model occasionally drops a period (e.g.
    transcribing "...BIRTH DEFECTS (2)..." instead of "...BIRTH DEFECTS.
    (2)..." — a punctuation-only transcription slip, not a labeling
    defect) which was failing genuinely compliant labels. The wording
    comparison now strips periods/commas before checking, while the
    all-caps/bold formatting checks remain zero-tolerance (see
    `_check_government_warning` in `verification.py`). All 4 sample
    labels now produce their intended verdict consistently across
    repeated live runs (`clean_match` and `casing_mismatch_ok` → PASS,
    `warning_titlecase_fail` and `abv_mismatch_fail` → FAIL), with
    extraction taking roughly 3-4 seconds per label end-to-end.
