"""
Label field extraction via a vision-capable LLM (Anthropic Claude).

Why an LLM instead of classic OCR + regex:
- Labels vary wildly in layout, font, and orientation. A general-purpose
  vision model handles that variance out of the box, whereas a
  regex-over-OCR-text pipeline would need constant tuning per label style.
- We still need it to report *formatting* cues (all-caps header, bold) for
  the government warning, not just the text — Jenny's interview flagged
  that agents reject labels for exactly this kind of formatting violation.
- Marcus (IT) flagged that outbound traffic to a lot of domains is
  firewalled in production. For the prototype we call the Anthropic API
  directly; a production deployment would need this endpoint added to an
  allowlist (documented in the README as a deployment consideration).

This module isolates the one outbound network call in the app behind a
single function so it's easy to swap providers (e.g. OpenAI GPT-4o,
Google Gemini) later without touching the verification logic.

Model choice and the 5-second bar: Sarah's interview was explicit that a
prior vendor pilot died because a single label took 30-40 seconds — agents
went back to eyeballing labels because it was faster. A larger/smarter
model is not obviously better here: this task is transcription, not
reasoning, and a slower model just burns the one budget stakeholders
actually care about. We default to a smaller, faster Claude model for
that reason, overridable via ANTHROPIC_VISION_MODEL if a deployment wants
to trade latency for accuracy on harder images.

Anti-hallucination rules in the prompt below are not boilerplate caution —
they encode specific, real failure modes: a vision model asked for "the
ABV" on a recognized brand will sometimes recall a *typical* proof/ABV for
that brand from training data rather than reading the actual bottle, and
will sometimes report a bottle's "Serving size" figure as its net contents.
Both are confident-sounding, wrong, and exactly the kind of silent error a
compliance tool cannot afford — an agent trusting a "match" verdict that
was actually a hallucinated guess is worse than no tool at all.
"""
import base64
import json
import os
import time

from anthropic import Anthropic

from app.models.schemas import ExtractedLabel

# Chosen for latency, not just cost: this is a verbatim-transcription task,
# not one requiring deep reasoning, so a smaller/faster model is the right
# fit for Sarah's "5 seconds or nobody uses it" requirement. Override via
# env var if a deployment prefers to trade latency for accuracy on
# especially difficult (angled/glared/low-light) images.
_MODEL = os.environ.get("ANTHROPIC_VISION_MODEL", "claude-haiku-4-5")

UNREADABLE = "UNREADABLE"

_SYSTEM_PROMPT = f"""You are assisting a federal (TTB) alcohol beverage compliance agent by \
transcribing text from a photo of a label. Read the image carefully and transcribe each \
field EXACTLY as printed — preserve original wording, punctuation, and capitalization. Do \
not correct, normalize, paraphrase, or "clean up" anything you read.

Fields to extract:
- brand_name
- class_type (the class/type designation, e.g. "Kentucky Straight Bourbon Whiskey")
- alcohol_content (as printed, e.g. "45% Alc./Vol. (90 Proof)", or a bare proof/degree \
statement like "80 Proof" or "151°")
- net_contents (as printed, e.g. "750 mL"). This is the TOTAL contents of the container. \
Some labels also print a separate "Serving size" figure (e.g. "Serving size: 150 mL") for \
nutritional/serving purposes — that is NOT net contents; never substitute it for the total \
container volume.
- bottler_info (name and address of the actual bottler/producer — the entity that made or \
bottled the product — as printed. Some labels ALSO print a second, unrelated address for an \
importer or distributor, often introduced by wording like "Imported by" — do not confuse \
that with the bottler/producer's own address.)
- country_of_origin (only if present, e.g. for imports — extract just the country name)
- government_warning_text (the FULL government health warning statement, verbatim, INCLUDING \
the leading "GOVERNMENT WARNING:" heading itself — that heading is part of the required \
statutory text, not a caption to omit, even though it's usually styled differently (bolded) \
from the sentences that follow)
- government_warning_is_all_caps_header (true only if the literal words "GOVERNMENT WARNING:" \
appear in all capital letters as printed on THIS label; false if any letter in that phrase is \
lowercase, e.g. "Government Warning:". This is standardized federal boilerplate you have seen \
many times in training — resist relying on memory of its "usual" appearance; look at what is \
actually printed in this specific image.)
- government_warning_appears_bold (true if that header visually appears bold/heavier weight \
than surrounding text on this label; false otherwise; null if you cannot tell)
- extraction_confidence ("high", "medium", or "low" based on overall image clarity/angle/glare)
- extraction_notes (brief note on anything ambiguous, illegible, or unusual — empty string if none)

Critical rules to avoid guessing:
1. Never invent a "typical" or "usually printed" value for a recognized brand. Read only \
what is actually visible in THIS image. If you cannot confidently read a field's value \
because of glare, blur, poor angle, or small text — even for a brand whose typical ABV or \
proof you might recognize from training — set that field's value to the literal string \
"{UNREADABLE}" rather than guessing. This applies especially to alcohol_content: do not \
recall a brand's usual proof/ABV instead of reading the bottle.
2. If a field is genuinely absent from the label (e.g. no country-of-origin statement on a \
domestic product), use an empty string "", not "{UNREADABLE}" — these mean different things: \
"{UNREADABLE}" means present but illegible (a re-photograph might fix it), "" means not on the \
label at all (no re-photograph will help).
3. Never guess at illegible fine print (especially a bottler street address) by \
pattern-matching to a plausible-sounding but different name/address/spelling — transcribe \
character-by-character what is actually visible, or mark it "{UNREADABLE}".

Respond with ONLY a single JSON object with exactly these keys, no markdown fences, no \
commentary."""


def _guess_media_type(filename: str) -> str:
    ext = (filename or "").rsplit(".", 1)[-1].lower()
    return {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
        "gif": "image/gif",
    }.get(ext, "image/jpeg")


def extract_label_fields(image_bytes: bytes, filename: str = "label.jpg") -> tuple[ExtractedLabel, int]:
    """Call the vision model and return (parsed fields, elapsed_ms).

    Raises RuntimeError on API failure or unparseable response so the
    caller can surface a clean error instead of a raw stack trace.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Add it to your .env file (see README) before running verification."
        )

    # Explicit timeout + a single retry: the Anthropic SDK's default
    # "Connection error." message swallows the real underlying cause
    # (DNS failure, TLS failure, read timeout, etc.), which makes
    # diagnosing serverless networking issues (e.g. on Vercel) painful.
    # We keep max_retries low here since our own caller (the /verify and
    # /extract routes) already has to stay under the platform's function
    # timeout.
    client = Anthropic(api_key=api_key, timeout=30.0, max_retries=1)
    media_type = _guess_media_type(filename)
    b64_image = base64.standard_b64encode(image_bytes).decode("utf-8")

    start = time.monotonic()
    try:
        response = client.messages.create(
            model=_MODEL,
            max_tokens=1024,
            system=_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": media_type, "data": b64_image},
                        },
                        {
                            "type": "text",
                            "text": "Extract the label fields as instructed and return only the JSON object.",
                        },
                    ],
                }
            ],
        )
    except Exception as exc:  # network/auth/rate-limit errors all land here
        # exc.__cause__ holds the real httpx/httpcore exception (e.g.
        # "getaddrinfo failed", "SSL: CERTIFICATE_VERIFY_FAILED", a real
        # timeout, etc.) that the Anthropic SDK's own message ("Connection
        # error.") hides. Surface both so the actual cause is visible in
        # the error the frontend displays, instead of just the generic
        # wrapper text.
        cause = exc.__cause__
        detail = f"{type(exc).__name__}: {exc}"
        if cause is not None:
            detail += f" | caused by {type(cause).__name__}: {cause}"
        raise RuntimeError(f"Vision extraction request failed: {detail}") from exc
    elapsed_ms = int((time.monotonic() - start) * 1000)

    raw_text = "".join(block.text for block in response.content if getattr(block, "type", None) == "text").strip()
    raw_text = _strip_markdown_fence(raw_text)

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Model returned non-JSON response: {raw_text[:300]}") from exc

    try:
        extracted = ExtractedLabel(**data)
    except Exception as exc:
        raise RuntimeError(f"Model JSON did not match expected schema: {exc}") from exc

    return extracted, elapsed_ms


def _strip_markdown_fence(text: str) -> str:
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return text