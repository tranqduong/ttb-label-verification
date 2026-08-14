"""
Maps a FieldResult's compliance status onto the reviewer-facing badge
vocabulary used by the Application Queue detail view (see routers/
applications.py and the frontend's field-card rendering).

This is deliberately kept separate from verification.py's compliance
logic: pass/fail/needs_review/not_applicable is what actually drives
overall_status and therefore the automated compliance verdict.
The badges here are a friendlier gloss over the *same* FieldResult data
for a human reviewer scanning a list of fields — "Minor Variance" reads
better next to an Expected/Detected pair than "pass (score 94%)", but it
must never change what the underlying verdict was.
"""
from app.models.schemas import FieldResult

# Substrings of FieldResult.reason that verification.py uses specifically
# for a "technically a pass, but not an exact match" case (casing/
# punctuation, unit conversion, a misread diacritic, a truncated/expanded
# corporate suffix). Matched against the lowercased reason text rather than
# adding a new status to verification.py, so the compliance logic itself
# doesn't have to know this presentation concept exists.
_MINOR_VARIANCE_MARKERS = (
    "minor formatting",
    "casing difference",
    "shortened or expanded",
    "unit differences",
    "unit conversion",
    "misread diacritic",
    "proof where application",
)


def field_badge(field: FieldResult) -> str:
    """Returns one of: match, minor_variance, on_label, needs_review,
    unreadable, mismatch."""
    if field.status == "not_applicable":
        # Application didn't file a value for this field, but the label has
        # one anyway — not a discrepancy, just informational.
        return "on_label"
    if field.status == "fail":
        return "mismatch"
    if field.status == "needs_review":
        if field.label_value == "(illegible on label)":
            return "unreadable"
        return "needs_review"
    # status == "pass"
    reason = field.reason.lower()
    if any(marker in reason for marker in _MINOR_VARIANCE_MARKERS):
        return "minor_variance"
    return "match"
