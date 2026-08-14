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

Minor Variance is deliberately restricted to identity/free-text fields
(brand name, bottler info/address, class-type, country of origin) —
per stakeholder feedback (cross-checked against a second reference
implementation of this brief), regulated/quantitative fields — alcohol
content, net contents, and the government warning — must present as a
clean Match or a hard Mismatch, never a softened "close enough." A label
that states "80 proof" against a filed "40%" is the *same* value once
converted, so it should read as a plain Match, not a variance; an actual
ABV or net-contents discrepancy already fails outright in
verification.py, so there's nothing left for this badge layer to soften.
The government warning is already strict, verbatim-or-fail in
verification.py (it has no "pass, but only after fuzzing" path today),
so _STRICT_FIELDS mostly documents and future-proofs that invariant here
rather than changing its current behavior.
"""
from app.models.schemas import FieldResult

# Fields where a "pass" is still allowed to be presented as "Minor
# Variance" when the underlying reason indicates a non-exact match
# (casing, punctuation, a shortened/expanded corporate suffix, etc).
# These are identity/free-text fields, not regulated quantities.
_LENIENT_FIELDS = frozenset({"brand_name", "bottler_info", "class_type", "country_of_origin"})

# Fields that must always resolve to a clean Match or Mismatch — no
# "Minor Variance" softening, regardless of what verification.py's reason
# text says. A pass here already means the values are equal (after unit
# conversion, where applicable); a genuine discrepancy is a "fail" status
# already, handled above before this set is ever consulted.
_STRICT_FIELDS = frozenset({"alcohol_content", "net_contents", "government_warning"})

# Substrings of FieldResult.reason that verification.py uses specifically
# for a "technically a pass, but not an exact match" case (casing/
# punctuation, unit conversion, a misread diacritic, a truncated/expanded
# corporate suffix). Matched against the lowercased reason text rather than
# adding a new status to verification.py, so the compliance logic itself
# doesn't have to know this presentation concept exists. Only consulted for
# fields in _LENIENT_FIELDS — see module docstring.
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
    if field.field in _STRICT_FIELDS:
        # Regulated/quantitative field: a pass is a clean match, full stop.
        return "match"
    if field.field in _LENIENT_FIELDS:
        reason = field.reason.lower()
        if any(marker in reason for marker in _MINOR_VARIANCE_MARKERS):
            return "minor_variance"
    return "match"
