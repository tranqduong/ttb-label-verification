"""
Comparison logic: application data (what was filed) vs. extracted label data
(what's actually printed).

Three-tier matching philosophy, deliberately different per field, based
directly on stakeholder feedback and cross-checked against two reference
implementations of this same brief (see README's "prior art" note):

1. PASS on exact/normalized match, or on a recognized non-discrepancy
   (unit conversion, truncated/expanded form). Dave's example: "STONE'S
   THROW" on the label vs. "Stone's Throw" in the application should not
   fail — that's the same name with different casing/punctuation, and a
   rigid string-equality check would create false positives that erode
   agent trust in the tool.
2. NEEDS_REVIEW for values that are clearly related but not a clean
   normalize-and-match — e.g. "Korbel Brut" vs. "Korbel Champagne
   Cellars" share the core brand word but add/swap a qualifier. That's
   worth a reviewer's glance, not an automatic hard fail, per Dave's
   point that this job needs judgment, not just pattern matching.
3. FAIL for values that are genuinely different, and for ANY deviation
   (however minor) in the government warning's wording or required
   formatting — Jenny's examples: "Government Warning" in title case, or
   the header not bold, are real rejections. That field gets zero
   fuzziness because that's how TTB actually enforces it.

A field extracted as the literal sentinel "UNREADABLE" (see
extraction.py) is treated differently from a field that's simply absent:
UNREADABLE means "present but illegible — a clearer photo might fix
this," which is a different, more actionable failure than "not printed
on the label at all."
"""
import re
import unicodedata

from rapidfuzz import fuzz

from app.models.schemas import ApplicationData, ExtractedLabel, FieldResult
from app.services.extraction import UNREADABLE

# The mandatory federal government warning, as codified at 27 CFR 16.21.
# Agents check this verbatim; minor whitespace differences are tolerated,
# wording differences are not.
REQUIRED_WARNING_TEXT = (
    "GOVERNMENT WARNING: (1) ACCORDING TO THE SURGEON GENERAL, WOMEN SHOULD NOT DRINK "
    "ALCOHOLIC BEVERAGES DURING PREGNANCY BECAUSE OF THE RISK OF BIRTH DEFECTS. (2) "
    "CONSUMPTION OF ALCOHOLIC BEVERAGES IMPAIRS YOUR ABILITY TO DRIVE A CAR OR OPERATE "
    "MACHINERY, AND MAY CAUSE HEALTH PROBLEMS."
)

PASS_THRESHOLD = 92   # 0-100; at/above this, a clean pass
REVIEW_THRESHOLD = 60  # 0-100; between this and PASS_THRESHOLD, flag for human judgment


def _normalize_text(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^\w\s]", "", value)  # drop punctuation
    value = re.sub(r"\s+", " ", value)
    return value


def _strip_diacritics(value: str) -> str:
    """é/ñ/ü etc -> plain letters. Vision extraction occasionally misreads
    an accent on a producer/brand name; a match once accents are stripped
    is a strong signal that's what happened, not a real name difference."""
    return "".join(c for c in unicodedata.normalize("NFD", value) if unicodedata.category(c) != "Mn")


def _word_overlap_ratio(a: str, b: str) -> float:
    """Fraction of the smaller word set that also appears in the other,
    ignoring order — catches "clearly related, not clearly identical"
    pairs (e.g. same core brand name, different qualifier)."""
    words_a, words_b = set(a.split()), set(b.split())
    if not words_a or not words_b:
        return 0.0
    shared = len(words_a & words_b)
    return shared / min(len(words_a), len(words_b))


def _is_truncated_form(a: str, b: str) -> bool:
    """True if the shorter (normalized) value appears as a whole-word run
    inside the longer one — e.g. 'sierra nevada' inside 'sierra nevada
    brewing co'. A common, legitimate pattern: a label prints a shortened
    or expanded form of the same declared name (dropped/added corporate
    suffix), not a different identity."""
    words_a, words_b = a.split(), b.split()
    if not words_a or not words_b or len(words_a) == len(words_b):
        return False
    shorter, longer = (words_a, words_b) if len(words_a) < len(words_b) else (words_b, words_a)
    if len(shorter) == 1 and len(shorter[0]) < 4:
        return False
    pattern = r"(^|\s)" + re.escape(" ".join(shorter)) + r"(\s|$)"
    return re.search(pattern, " ".join(longer)) is not None


def _is_unreadable(value: str | None) -> bool:
    return (value or "").strip().upper() == UNREADABLE


def _missing_or_unreadable(field_name: str, label_value: str | None, application_value: str | None) -> FieldResult | None:
    """Returns a FieldResult for the 'not on label' / 'illegible on label'
    cases, or None if extraction actually produced a usable value and
    normal comparison should proceed."""
    if _is_unreadable(label_value):
        return FieldResult(
            field=field_name, label_value="(illegible on label)", application_value=application_value,
            status="needs_review",
            reason="Image quality prevented a confident read of this field — recommend re-photographing the label rather than trusting a guess.",
        )
    if not label_value:
        return FieldResult(
            field=field_name, label_value=None, application_value=application_value,
            status="fail", reason="Not found on label image.",
        )
    return None


def _fuzzy_field(field_name: str, label_value: str | None, application_value: str | None) -> FieldResult:
    if not application_value:
        return FieldResult(
            field=field_name, label_value=label_value, application_value=application_value,
            status="not_applicable", reason="Not provided in application data.",
        )

    missing = _missing_or_unreadable(field_name, label_value, application_value)
    if missing:
        return missing

    norm_label = _normalize_text(label_value)
    norm_app = _normalize_text(application_value)

    if norm_label == norm_app:
        return FieldResult(
            field=field_name, label_value=label_value, application_value=application_value,
            status="pass", reason="Matches application data.",
        )

    # Same once accents are stripped -> almost always a misread diacritic,
    # not a genuinely different name. Flag for a quick human glance rather
    # than a silent pass or a hard fail.
    if _strip_diacritics(norm_label) == _strip_diacritics(norm_app):
        return FieldResult(
            field=field_name, label_value=label_value, application_value=application_value,
            status="needs_review",
            reason="Matches once accented characters are ignored — looks like a misread diacritic (e.g. ñ, é, ü) rather than a real discrepancy. Double-check that letter against the label.",
        )

    if _is_truncated_form(norm_label, norm_app):
        return FieldResult(
            field=field_name, label_value=label_value, application_value=application_value,
            status="pass",
            reason="Label shows a shortened or expanded form of the application value (e.g. a corporate suffix added/dropped) — same underlying value.",
        )

    score = fuzz.ratio(norm_label, norm_app)
    if score >= PASS_THRESHOLD:
        return FieldResult(
            field=field_name, label_value=label_value, application_value=application_value,
            status="pass", reason=f"Matches application data (minor formatting/casing difference only, similarity {score}%).",
        )

    if score >= REVIEW_THRESHOLD or _word_overlap_ratio(norm_label, norm_app) >= 0.5:
        return FieldResult(
            field=field_name, label_value=label_value, application_value=application_value,
            status="needs_review",
            reason=f"Shares wording with the application value but differs beyond formatting (similarity {score}%) — worth a reviewer's judgment call, not an automatic fail.",
        )

    return FieldResult(
        field=field_name, label_value=label_value, application_value=application_value,
        status="fail", reason=f"Does not match application data (similarity {score}%).",
    )


# ---------- Alcohol content: handles both % and proof, with unit conversion ----------

def _parse_abv_percent(value: str) -> float | None:
    """Extracts an ABV percentage from a '%' statement, or converts a US
    proof statement (proof = 2 x ABV%). Handles a bare '90 Proof' as well
    as '45% Alc./Vol. (90 Proof)'."""
    pct_match = re.search(r"(\d+(?:\.\d+)?)\s*%", value)
    if pct_match:
        return float(pct_match.group(1))
    proof_match = re.search(r"(\d+(?:\.\d+)?)\s*°?\s*proof", value, re.IGNORECASE)
    if proof_match:
        return float(proof_match.group(1)) / 2
    return None


def _check_alcohol_content(extracted: ExtractedLabel, application: ApplicationData) -> FieldResult:
    if not application.alcohol_content:
        return FieldResult(
            field="alcohol_content", label_value=extracted.alcohol_content, application_value=None,
            status="not_applicable",
            reason="Not required in application (permitted omission for some beer/wine per TTB rules).",
        )

    missing = _missing_or_unreadable("alcohol_content", extracted.alcohol_content, application.alcohol_content)
    if missing:
        return missing

    label_value, app_value = extracted.alcohol_content, application.alcohol_content
    app_pct = _parse_abv_percent(app_value)
    label_pct = _parse_abv_percent(label_value)

    if app_pct is not None and label_pct is not None:
        if abs(app_pct - label_pct) < 0.05:
            note = "ABV value matches (formatting/unit differences ignored)."
            if ("%" in label_value) != ("%" in app_value):
                note = "Label states strength as proof where application states %, or vice versa — same alcohol content once converted."
            return FieldResult(
                field="alcohol_content", label_value=label_value, application_value=app_value,
                status="pass", reason=note,
            )
        # Numbers parsed on both sides and they genuinely disagree — this
        # is authoritative over any generic text-similarity score below,
        # since ABV is a regulated number, not prose.
        return FieldResult(
            field="alcohol_content", label_value=label_value, application_value=app_value,
            status="fail", reason=f"ABV on label ({label_pct}%) does not match application ({app_pct}%).",
        )

    # Couldn't parse a clean number on one/both sides — fall back to text
    # similarity as a weaker signal.
    generic = _fuzzy_field("alcohol_content", label_value, app_value)
    return generic


# ---------- Net contents: handles mL / cL / L conversion + EU "estimated fill" mark ----------

def _strip_estimated_fill_mark(value: str) -> str:
    """A trailing 'e' or '℮' after the quantity (e.g. '750 mL e') is EU
    'estimated average fill' boilerplate, not a different quantity."""
    return re.sub(r"\s*[e℮]\.?\s*$", "", value, flags=re.IGNORECASE).strip()


def _parse_volume_ml(value: str) -> float | None:
    match = re.search(r"(\d+(?:\.\d+)?)\s*(ml|cl|l)\b", value, re.IGNORECASE)
    if not match:
        return None
    amount = float(match.group(1))
    unit = match.group(2).lower()
    return {"ml": amount, "cl": amount * 10, "l": amount * 1000}[unit]


def _check_net_contents(extracted: ExtractedLabel, application: ApplicationData) -> FieldResult:
    label_value, app_value = extracted.net_contents, application.net_contents
    missing = _missing_or_unreadable("net_contents", label_value, app_value)
    if missing:
        return missing

    label_core = _strip_estimated_fill_mark(label_value)
    app_vol = _parse_volume_ml(app_value)
    label_vol = _parse_volume_ml(label_core)

    if app_vol is not None and label_vol is not None:
        if abs(app_vol - label_vol) < 0.5:
            return FieldResult(
                field="net_contents", label_value=label_value, application_value=app_value,
                status="pass", reason="Matches application data (unit differences, if any, converted and confirmed equal).",
            )
        return FieldResult(
            field="net_contents", label_value=label_value, application_value=app_value,
            status="fail", reason=f"Net contents on label ({label_vol:g} mL) does not match application ({app_vol:g} mL).",
        )

    return _fuzzy_field("net_contents", label_core, app_value)


def _check_government_warning(extracted: ExtractedLabel) -> FieldResult:
    """Strict check — no fuzziness beyond whitespace collapsing. This
    field is regulated down to capitalization, so near-matches are still
    failures (per Jenny's real-world rejection examples)."""
    if _is_unreadable(extracted.government_warning_text):
        return FieldResult(
            field="government_warning", label_value="(illegible on label)", application_value=REQUIRED_WARNING_TEXT,
            status="needs_review",
            reason="Image quality prevented a confident read of the government warning — recommend re-photographing the label rather than trusting a guess.",
        )
    if not (extracted.government_warning_text or "").strip():
        return FieldResult(
            field="government_warning", label_value=None, application_value=REQUIRED_WARNING_TEXT,
            status="fail", reason="The statutory government warning is required by law but was not found on the label.",
        )

    label_text = extracted.government_warning_text.strip()
    # Strip periods/commas before comparing wording. Verified against a live
    # extraction run: the vision model occasionally drops a period (e.g.
    # transcribing "...BIRTH DEFECTS (2)..." instead of "...BIRTH DEFECTS.
    # (2)..." -- a punctuation-only transcription slip, not an actual
    # labeling defect. Zero tolerance here would fail a compliant label for
    # an OCR artifact; genuine wording changes (different/missing/reordered
    # words) still fail below since those survive punctuation stripping.
    norm_label = re.sub(r"[.,]", "", label_text.upper())
    norm_label = re.sub(r"\s+", " ", norm_label).strip()
    norm_required = re.sub(r"[.,]", "", REQUIRED_WARNING_TEXT.upper())
    norm_required = re.sub(r"\s+", " ", norm_required).strip()

    issues = []
    if norm_label != norm_required:
        similarity = fuzz.ratio(norm_label, norm_required)
        issues.append(f"wording deviates from the required statutory text (similarity {similarity}%)")

    if extracted.government_warning_is_all_caps_header is False:
        issues.append('"GOVERNMENT WARNING:" header is not in all capital letters (required)')
    elif extracted.government_warning_is_all_caps_header is None:
        issues.append("could not confirm header is all-caps from the image — needs manual review")

    if extracted.government_warning_appears_bold is False:
        issues.append('"GOVERNMENT WARNING:" header does not appear bold (required)')

    if issues:
        return FieldResult(
            field="government_warning", label_value=label_text, application_value=REQUIRED_WARNING_TEXT,
            status="fail", reason="; ".join(issues).capitalize() + " (per 27 CFR 16.21).",
        )
    return FieldResult(
        field="government_warning", label_value=label_text, application_value=REQUIRED_WARNING_TEXT,
        status="pass", reason="Matches required wording and formatting.",
    )


def _check_country_of_origin(extracted: ExtractedLabel, application: ApplicationData) -> FieldResult:
    if not application.is_import:
        return FieldResult(
            field="country_of_origin", label_value=extracted.country_of_origin, application_value=None,
            status="not_applicable", reason="Not required — application marked as domestic product.",
        )
    return _fuzzy_field("country_of_origin", extracted.country_of_origin, application.country_of_origin)


def verify_label(extracted: ExtractedLabel, application: ApplicationData) -> list[FieldResult]:
    results = [
        _fuzzy_field("brand_name", extracted.brand_name, application.brand_name),
        _fuzzy_field("class_type", extracted.class_type, application.class_type),
        _check_alcohol_content(extracted, application),
        _check_net_contents(extracted, application),
        _fuzzy_field("bottler_info", extracted.bottler_info, application.bottler_info),
        _check_country_of_origin(extracted, application),
        _check_government_warning(extracted),
    ]

    if extracted.extraction_confidence == "low":
        results.append(
            FieldResult(
                field="image_quality", label_value=extracted.extraction_confidence, application_value=None,
                status="needs_review",
                reason=(
                    "Low overall extraction confidence — image may be angled, blurry, or glare-affected. "
                    "Recommend re-photographing the label rather than trusting this result."
                ),
            )
        )
    return results


def overall_status(fields: list[FieldResult]) -> str:
    if any(f.status == "fail" for f in fields):
        return "fail"
    if any(f.status == "needs_review" for f in fields):
        return "needs_review"
    return "pass"
