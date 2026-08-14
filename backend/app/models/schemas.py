"""
Pydantic models shared across the app.

Design note: TTB label requirements vary somewhat by beverage class (beer /
wine / distilled spirits) but the prototype targets the common-denominator
field set called out in the take-home brief. `beverage_type` is captured so
the UI/README can be explicit about that scoping decision rather than
silently pretending one schema covers every TTB edge case.
"""
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class BeverageType(str, Enum):
    beer = "beer"
    wine = "wine"
    distilled_spirits = "distilled_spirits"
    other = "other"


class ApplicationData(BaseModel):
    """What the compliance agent enters/uploads from the COLA application."""

    beverage_type: BeverageType = BeverageType.distilled_spirits
    brand_name: str = Field(..., description="Brand name as filed in the application")
    class_type: str = Field(..., description="Class/type designation, e.g. 'Kentucky Straight Bourbon Whiskey'")
    alcohol_content: Optional[str] = Field(
        None, description="ABV as filed, e.g. '45% Alc./Vol.' — optional for some beer/wine per TTB rules"
    )
    net_contents: str = Field(..., description="Net contents as filed, e.g. '750 mL'")
    bottler_info: Optional[str] = Field(None, description="Name and address of bottler/producer as filed")
    country_of_origin: Optional[str] = Field(None, description="Required for imports only")
    is_import: bool = False


class FieldResult(BaseModel):
    field: str
    label_value: Optional[str] = None
    application_value: Optional[str] = None
    status: str  # "pass" | "fail" | "warning" | "not_applicable"
    reason: str


class ExtractedLabel(BaseModel):
    """Raw structured output from the vision extraction step."""

    brand_name: Optional[str] = None
    class_type: Optional[str] = None
    alcohol_content: Optional[str] = None
    net_contents: Optional[str] = None
    bottler_info: Optional[str] = None
    country_of_origin: Optional[str] = None
    government_warning_text: Optional[str] = None
    government_warning_is_all_caps_header: Optional[bool] = None
    government_warning_appears_bold: Optional[bool] = None
    extraction_confidence: Optional[str] = None  # "high" | "medium" | "low"
    extraction_notes: Optional[str] = None


class VerificationResult(BaseModel):
    overall_status: str  # "pass" | "fail" | "needs_review"
    fields: list[FieldResult]
    extracted: ExtractedLabel
    processing_time_ms: int
    label_filename: Optional[str] = None


class BatchItemResult(BaseModel):
    index: int
    label_filename: str
    result: Optional[VerificationResult] = None
    error: Optional[str] = None


class BatchVerificationResult(BaseModel):
    total: int
    passed: int
    failed: int
    needs_review: int
    errored: int
    items: list[BatchItemResult]
    processing_time_ms: int


class ApplicationStatus(str, Enum):
    """Review workflow state for a persisted Application Queue entry.

    Distinct from VerificationResult.overall_status (pass/fail/needs_review),
    which is the *automated* compliance verdict. This is the *human
    reviewer's* disposition on that verdict:
      - needs_review: default state for every fresh submission — nobody has
        acted on it yet, regardless of what the automated verdict said.
      - flagged: reviewer wants a second set of eyes / escalation before
        deciding either way.
      - pending: reviewer rejected the label — read as "pending
        resubmission from the applicant" (a corrected label/application),
        not a dead end.
      - approved: reviewer signed off.
    """

    needs_review = "needs_review"
    flagged = "flagged"
    pending = "pending"
    approved = "approved"


class UpdateApplicationStatusRequest(BaseModel):
    status: ApplicationStatus
    note: Optional[str] = Field(None, description="Reviewer's note — required by the frontend for flag/reject.")
