# Security and Threat Model

This document describes what this prototype's trust boundaries are, what data it handles, and the threats considered against it -- plus what's deliberately out of scope at this stage. It follows a STRIDE-style threat model (Spoofing, Tampering, Repudiation, Information Disclosure, Denial of Service, Elevation of Privilege), scoped to how this specific app is actually built rather than a generic checklist.

## System overview

```
Browser (vanilla JS, single-page frontend)
  | fetch("/api/...") -- same-origin, no auth token
  v
FastAPI serverless function (api/index.py -> app/main.py)
  |
  |--> Postgres / Neon (asyncpg) -- application data, field results,
  |      and the label image bytes themselves (BYTEA column)
  |
  '--> Anthropic API (claude-haiku-4-5, overridable via
         ANTHROPIC_VISION_MODEL) -- label image + prompt sent out
         for vision extraction, response returned as structured JSON
```

There is exactly one trust boundary that matters here: **the browser is untrusted.** Anything a client sends -- uploaded label images, the `application_data` JSON on a submission, an application ID in a URL path, a reviewer note -- is treated as attacker-controlled input. The Anthropic API is treated as a trusted third-party processor for the one piece of data it receives (the label image plus extraction prompt); Postgres is treated as a trusted store reachable only from this backend, never directly from the browser.

## Threats considered

**Spoofing.** There is no authentication anywhere in this prototype -- every endpoint under `/api` is reachable by anyone who has the deployment's URL, with no notion of "who" is calling. There's nothing to spoof yet because there's no identity to begin with; this is the single biggest gap before handling real filed applications (see "Out of scope" below).

**Tampering.** Any client can `PATCH /applications/{id}` to change a submission's review status or note, or `POST /applications/{id}/image` to replace its label photo, with no check that the caller is the reviewer who should own that action. Application IDs are UUIDs (not sequential), which limits casual guessing but is obscurity, not access control -- anyone who has or guesses an ID has full read/write on that record.

**Repudiation.** No user identity is captured anywhere, so there is no audit trail of who moved a submission to `approved` or who left a given reviewer note -- only that it happened, and when.

**Information disclosure.** Label images and the filed application data they're checked against are stored as-is in Postgres (image bytes in a `BYTEA` column). `GET /applications/{id}/image` will serve that image back to any caller who has the ID, with no ownership check. Database credentials are wired up automatically through Vercel's Postgres storage integration (see `db.py`) rather than being typed in by hand, which avoids one common leak vector (a credential pasted into a config field or committed to the repo), but the data itself is not encrypted at the application layer beyond whatever Neon/Vercel provide by default. The Anthropic API key is read from an environment variable and is explicitly redacted before any exception text reaches a client response, so a failed vision call can't leak it back to the browser.

**Denial of service.** The batch-upload path (`/api/verify/batch` and its streaming variant) caps concurrent vision-extraction calls at `MAX_CONCURRENT_EXTRACTIONS = 6` specifically so a large batch (Sarah's interview described 200-300 labels at once) can't hammer the Anthropic API or blow through its rate limits. Each individual vision call has a 30-second timeout and one retry. There is no rate limiting on the API endpoints themselves beyond that semaphore, so a client could still submit an unbounded number of separate requests.

**Elevation of privilege.** Not applicable yet -- there are no privilege levels in this prototype (see Spoofing above). This becomes relevant the moment any form of login is added.

## Out of scope for this prototype

- **No user accounts, login, or roles.** Every visitor to the Application Queue can see and modify every submission. A real deployment needs auth in front of the Queue and per-action attribution before it could be trusted with real, non-test filed applications.
- **No live COLA integration.** Filed "application data" here is whatever a reviewer types into the form, not pulled from or verified against TTB's actual COLA system -- Marcus's (IT) interview flagged that this app has no live connection to that system, by design, for a prototype.
- **No malware/virus scanning of uploaded images**, beyond basic empty-file and content-type checks.
- **No network allowlisting configured.** Marcus also flagged that production egress is firewalled to an allowlist of domains; this prototype calls the Anthropic API directly. A real deployment would need that endpoint added to the allowlist rather than assuming open egress.
- **No encryption-at-rest guarantee** beyond whatever the underlying Neon/Vercel infrastructure provides by default.

None of the above is a claim that this prototype is production-hardened -- it's meant to make explicit exactly which hardening steps a real deployment would still need, so that gap is a documented decision rather than a silent one.
