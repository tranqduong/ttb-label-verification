"""
Unit tests for the pure comparison logic in app.services.verification.
These do NOT call the vision API — they construct ExtractedLabel objects
directly, so they run offline and fast (good for CI / no API key needed).

Run with: python3 -m pytest tests/test_verification.py -v
(from the backend/ directory, or with backend/ on PYTHONPATH)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.models.schemas import ApplicationData, ExtractedLabel  # noqa: E402
from app.services.verification import (  # noqa: E402
    REQUIRED_WARNING_TEXT,
    overall_status,
    verify_label,
)


def _field(fields, name):
    return next(f for f in fields if f.field == name)


def test_exact_match_passes_everything():
    application = ApplicationData(
        brand_name="Old Tom Distillery",
        class_type="Kentucky Straight Bourbon Whiskey",
        alcohol_content="45% Alc./Vol.",
        net_contents="750 mL",
        bottler_info="Old Tom Distillery, Bardstown, KY",
    )
    extracted = ExtractedLabel(
        brand_name="Old Tom Distillery",
        class_type="Kentucky Straight Bourbon Whiskey",
        alcohol_content="45% Alc./Vol. (90 Proof)",
        net_contents="750 mL",
        bottler_info="Old Tom Distillery, Bardstown, KY",
        government_warning_text=REQUIRED_WARNING_TEXT,
        government_warning_is_all_caps_header=True,
        government_warning_appears_bold=True,
        extraction_confidence="high",
    )
    fields = verify_label(extracted, application)
    assert overall_status(fields) == "pass"


def test_casing_and_punctuation_difference_still_passes():
    """Dave's example: 'STONE'S THROW' vs 'Stone's Throw' should not fail."""
    application = ApplicationData(
        brand_name="Stone's Throw",
        class_type="American Craft Vodka",
        net_contents="750 mL",
    )
    extracted = ExtractedLabel(
        brand_name="STONE'S THROW",
        class_type="AMERICAN CRAFT VODKA",
        net_contents="750 mL",
        government_warning_text=REQUIRED_WARNING_TEXT,
        government_warning_is_all_caps_header=True,
        government_warning_appears_bold=True,
    )
    fields = verify_label(extracted, application)
    assert _field(fields, "brand_name").status == "pass"
    assert _field(fields, "class_type").status == "pass"


def test_genuinely_different_brand_name_fails():
    application = ApplicationData(brand_name="Stone's Throw", class_type="Vodka", net_contents="750 mL")
    extracted = ExtractedLabel(brand_name="Stony Brook Spirits", class_type="Vodka", net_contents="750 mL")
    fields = verify_label(extracted, application)
    assert _field(fields, "brand_name").status == "fail"


def test_government_warning_titlecase_fails():
    """Jenny's example: 'Government Warning' in title case should be rejected."""
    application = ApplicationData(brand_name="Harbor Light Brewing", class_type="IPA", net_contents="355 mL")
    extracted = ExtractedLabel(
        brand_name="Harbor Light Brewing",
        class_type="IPA",
        net_contents="355 mL",
        government_warning_text=REQUIRED_WARNING_TEXT.title(),
        government_warning_is_all_caps_header=False,
        government_warning_appears_bold=False,
    )
    fields = verify_label(extracted, application)
    warning = _field(fields, "government_warning")
    assert warning.status == "fail"
    assert overall_status(fields) == "fail"


def test_government_warning_exact_passes():
    application = ApplicationData(brand_name="Harbor Light Brewing", class_type="IPA", net_contents="355 mL")
    extracted = ExtractedLabel(
        brand_name="Harbor Light Brewing",
        class_type="IPA",
        net_contents="355 mL",
        government_warning_text=REQUIRED_WARNING_TEXT,
        government_warning_is_all_caps_header=True,
        government_warning_appears_bold=True,
    )
    fields = verify_label(extracted, application)
    assert _field(fields, "government_warning").status == "pass"


def test_government_warning_dropped_period_still_passes():
    """Verified against a live extraction run: the vision model sometimes
    drops a period (e.g. 'BIRTH DEFECTS (2)' instead of 'BIRTH DEFECTS.
    (2)') -- a punctuation-only OCR slip, not a real labeling defect, and
    it should not fail a compliant label."""
    application = ApplicationData(brand_name="Foo", class_type="Bourbon", net_contents="750 mL")
    warning_missing_period = REQUIRED_WARNING_TEXT.replace("DEFECTS. (2)", "DEFECTS (2)")
    extracted = ExtractedLabel(
        brand_name="Foo", class_type="Bourbon", net_contents="750 mL",
        government_warning_text=warning_missing_period,
        government_warning_is_all_caps_header=True, government_warning_appears_bold=True,
    )
    fields = verify_label(extracted, application)
    assert _field(fields, "government_warning").status == "pass"


def test_abv_mismatch_fails():
    application = ApplicationData(
        brand_name="Silver Ridge Vineyards", class_type="Cabernet Sauvignon",
        alcohol_content="13.0% Alc./Vol.", net_contents="750 mL",
    )
    extracted = ExtractedLabel(
        brand_name="Silver Ridge Vineyards", class_type="Cabernet Sauvignon",
        alcohol_content="14.5% Alc./Vol.", net_contents="750 mL",
        government_warning_text=REQUIRED_WARNING_TEXT,
        government_warning_is_all_caps_header=True, government_warning_appears_bold=True,
    )
    fields = verify_label(extracted, application)
    assert _field(fields, "alcohol_content").status == "fail"
    assert overall_status(fields) == "fail"


def test_missing_field_on_label_fails():
    application = ApplicationData(brand_name="Foo", class_type="Bar", net_contents="750 mL")
    extracted = ExtractedLabel(brand_name=None, class_type="Bar", net_contents="750 mL")
    fields = verify_label(extracted, application)
    assert _field(fields, "brand_name").status == "fail"


def test_optional_country_of_origin_not_applicable_when_domestic():
    application = ApplicationData(brand_name="Foo", class_type="Bar", net_contents="750 mL", is_import=False)
    extracted = ExtractedLabel(brand_name="Foo", class_type="Bar", net_contents="750 mL")
    fields = verify_label(extracted, application)
    assert _field(fields, "country_of_origin").status == "not_applicable"


def test_low_confidence_extraction_flags_review():
    application = ApplicationData(brand_name="Foo", class_type="Bar", net_contents="750 mL")
    extracted = ExtractedLabel(
        brand_name="Foo", class_type="Bar", net_contents="750 mL",
        government_warning_text=REQUIRED_WARNING_TEXT,
        government_warning_is_all_caps_header=True, government_warning_appears_bold=True,
        extraction_confidence="low",
    )
    fields = verify_label(extracted, application)
    assert overall_status(fields) == "needs_review"


def test_proof_vs_percent_abv_recognized_as_same_value():
    """A label stating strength purely as proof (e.g. '90 Proof') is the
    same alcohol content as an application filed as '45% Alc./Vol.' --
    proof is exactly double the ABV percentage."""
    application = ApplicationData(brand_name="Foo", class_type="Bourbon", alcohol_content="45% Alc./Vol.", net_contents="750 mL")
    extracted = ExtractedLabel(brand_name="Foo", class_type="Bourbon", alcohol_content="90 Proof", net_contents="750 mL")
    fields = verify_label(extracted, application)
    assert _field(fields, "alcohol_content").status == "pass"


def test_metric_volume_unit_conversion_recognized_as_same_value():
    """'70 CL' on the label and '700 mL' filed in the application are the
    same volume once converted -- not a discrepancy."""
    application = ApplicationData(brand_name="Foo", class_type="Wine", net_contents="700 mL")
    extracted = ExtractedLabel(brand_name="Foo", class_type="Wine", net_contents="70 CL")
    fields = verify_label(extracted, application)
    assert _field(fields, "net_contents").status == "pass"


def test_eu_estimated_fill_mark_stripped_before_comparison():
    application = ApplicationData(brand_name="Foo", class_type="Wine", net_contents="750 mL")
    extracted = ExtractedLabel(brand_name="Foo", class_type="Wine", net_contents="750 mL e")
    fields = verify_label(extracted, application)
    assert _field(fields, "net_contents").status == "pass"


def test_unreadable_field_flagged_needs_review_not_hard_fail():
    """A field the vision model marks UNREADABLE (illegible due to glare/
    angle/blur) should be routed to human review, not silently failed or
    silently passed -- and the reason should point at a re-photograph."""
    application = ApplicationData(brand_name="Foo", class_type="Bar", net_contents="750 mL")
    extracted = ExtractedLabel(
        brand_name="UNREADABLE", class_type="Bar", net_contents="750 mL",
        government_warning_text=REQUIRED_WARNING_TEXT,
        government_warning_is_all_caps_header=True, government_warning_appears_bold=True,
    )
    fields = verify_label(extracted, application)
    brand = _field(fields, "brand_name")
    assert brand.status == "needs_review"
    assert overall_status(fields) == "needs_review"


def test_diacritic_only_difference_flagged_needs_review():
    """A misread accent (e.g. 'La Rojeña' read as 'La Rojenia') should be
    distinguished from a genuinely different name -- flagged for a quick
    human glance rather than passed silently or failed outright."""
    application = ApplicationData(brand_name="La Rojena", class_type="Tequila", net_contents="750 mL")
    extracted = ExtractedLabel(brand_name="La Rojeña", class_type="Tequila", net_contents="750 mL")
    fields = verify_label(extracted, application)
    assert _field(fields, "brand_name").status == "needs_review"


def test_truncated_corporate_suffix_still_passes():
    """A label reading 'Sierra Nevada' for an application declared as
    'Sierra Nevada Brewing Co.' is unambiguously the same brand -- a
    dropped corporate suffix, not a different identity."""
    application = ApplicationData(brand_name="Sierra Nevada Brewing Co.", class_type="Pale Ale", net_contents="355 mL")
    extracted = ExtractedLabel(brand_name="Sierra Nevada", class_type="Pale Ale", net_contents="355 mL")
    fields = verify_label(extracted, application)
    assert _field(fields, "brand_name").status == "pass"


def test_partially_related_name_flagged_needs_review_not_hard_fail():
    """'Korbel Brut' vs. an application declared as 'Korbel Champagne
    Cellars' shares the core brand word but swaps a qualifier -- worth a
    reviewer's glance, not an automatic hard mismatch."""
    application = ApplicationData(brand_name="Korbel Champagne Cellars", class_type="Champagne", net_contents="750 mL")
    extracted = ExtractedLabel(brand_name="Korbel Brut", class_type="Champagne", net_contents="750 mL")
    fields = verify_label(extracted, application)
    assert _field(fields, "brand_name").status == "needs_review"
