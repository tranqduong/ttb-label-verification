"""
Application Queue endpoints.

/verify (in verify.py) is stateless — it processes one upload and returns
the result, nothing is kept. This router adds a persisted counterpart for
Single Label Review submissions, split into two steps rather than one:

  1. POST /applications saves the label image + filed application data to
     the queue as `pending_analysis` — no vision extraction happens yet.
  2. POST /applications/{id}/analyze runs extraction + comparison against
     the image already on file and moves the record to `needs_review`.

Splitting these (rather than doing both in one request, which is how this
used to work) mirrors a second reference implementation of this brief: a
reviewer can open a queue full of saved submissions and see each one's
label image immediately, without every single one having paid for a
vision-extraction call just to land in the queue. Analysis only runs when
a reviewer actually clicks "Analyze Label" on the one they're looking at.

Scope note: only Single Label Review submissions are queued. Batch Upload
stays the stateless concurrent-triage tool it already was — queuing 300
batch items would need pagination/bulk-action UI that's out of scope for
this pass, and batch's own CSV export already covers "get all these
results out."
"""
import asyncio
import json

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from app.models.schemas import ApplicationData, FieldResult, UpdateApplicationStatusRequest
from app.services import db
from app.services.badges import field_badge
from app.services.extraction import extract_label_fields
from app.services.verification import overall_status, verify_label

router = APIRouter()

_BEVERAGE_LABELS = {
    "beer": "Malt Beverage",
    "wine": "Wine",
    "distilled_spirits": "Distilled Spirits",
    "other": "Other",
}


def _with_badges(record: dict) -> dict:
    """Attaches a presentation-only `badge` key to each field result (see
    badges.py) without touching the stored field_results themselves."""
    fields_with_badges = []
    for f in record["fields"]:
        fields_with_badges.append({**f, "badge": field_badge(FieldResult(**f))})
    return {**record, "fields": fields_with_badges}


def _summarize(record: dict) -> dict:
    app_data = record["application"]
    details_parts = [_BEVERAGE_LABELS.get(app_data.get("beverage_type"), app_data.get("beverage_type"))]
    if app_data.get("alcohol_content"):
        details_parts.append(app_data["alcohol_content"])
    return {
        "id": record["id"],
        "display_id": record.get("display_id"),
        "created_at": record["created_at"],
        "status": record["status"],
        "brand_name": app_data.get("brand_name"),
        "class_type": app_data.get("class_type"),
        "details": " · ".join(p for p in details_parts if p),
        "net_contents": app_data.get("net_contents"),
        "is_import": app_data.get("is_import", False),
        "overall_status": record["overall_status"],
    }


@router.post("/applications", status_code=201)
async def submit_application(
    label_image: UploadFile = File(...),
    application_data: str = Form(..., description="JSON-encoded ApplicationData"),
):
    """Saves the label image + filed application data to the Application
    Queue as `pending_analysis` -- no extraction runs here. See
    analyze_application() below for the step that actually compares the
    image against the filed data."""
    try:
        application = ApplicationData(**json.loads(application_data))
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid application_data JSON: {exc}")

    image_bytes = await label_image.read()
    if not image_bytes:
        raise HTTPException(status_code=422, detail="Uploaded label image is empty.")

    try:
        record = await db.insert_pending_application(
            application_data=application.model_dump(mode="json"),
            image_bytes=image_bytes,
            label_filename=label_image.filename or "label.jpg",
        )
    except RuntimeError as exc:
        # No Postgres storage provisioned yet -- see db.py's error message.
        raise HTTPException(status_code=503, detail=str(exc))

    return _with_badges(record)


@router.post("/applications/{app_id}/analyze")
async def analyze_application(app_id: str):
    """Runs vision extraction + comparison against the image already on
    file for this application (saved via POST /applications above), then
    persists the result and moves the record from `pending_analysis` to
    `needs_review`. Safe to call more than once -- each call just re-runs
    extraction and overwrites the previous result."""
    try:
        image = await db.get_application_image(app_id)
        record = await db.get_application(app_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if not record:
        raise HTTPException(status_code=404, detail="Application not found.")
    if not image:
        raise HTTPException(status_code=404, detail="This application has no label image on file.")
    image_bytes, filename = image

    application = ApplicationData(**record["application"])
    try:
        extracted, elapsed_ms = await asyncio.to_thread(
            extract_label_fields, image_bytes, filename or "label.jpg"
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    fields = verify_label(extracted, application)
    status = overall_status(fields)

    try:
        updated = await db.complete_analysis(
            app_id,
            extracted=extracted.model_dump(mode="json"),
            field_results=[f.model_dump(mode="json") for f in fields],
            overall_status=status,
            analysis_elapsed_ms=elapsed_ms,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if not updated:
        raise HTTPException(status_code=404, detail="Application not found.")

    return _with_badges(updated)


@router.get("/applications/{app_id}/image")
async def get_application_image(app_id: str):
    """Serves back the label photo saved with a submission, so the queue
    detail view can show it before (and after) analysis has run."""
    try:
        image = await db.get_application_image(app_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if not image:
        raise HTTPException(status_code=404, detail="No image on file for this application.")
    image_bytes, filename = image
    media_type = "image/png" if (filename or "").lower().endswith(".png") else "image/jpeg"
    return Response(content=image_bytes, media_type=media_type)


@router.post("/applications/{app_id}/image")
async def replace_application_image(app_id: str, label_image: UploadFile = File(...)):
    """Swaps in a new label photo for an application and resets it to
    `pending_analysis`, clearing the prior analysis result and reviewer
    note (both applied to the old photo). Backs the reviewer-facing
    "Request re-upload / Replace image" action -- both the whole-submission
    control and the per-field "Request re-upload" action shown on an
    Unreadable field call this same endpoint."""
    image_bytes = await label_image.read()
    if not image_bytes:
        raise HTTPException(status_code=422, detail="Uploaded label image is empty.")
    try:
        updated = await db.replace_image(app_id, image_bytes, label_image.filename or "label.jpg")
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if not updated:
        raise HTTPException(status_code=404, detail="Application not found.")
    return _with_badges(updated)


@router.get("/applications")
async def list_applications():
    try:
        records = await db.list_applications()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    items = [_summarize(r) for r in records]
    counts = {
        "all": len(items),
        "pending_analysis": sum(1 for i in items if i["status"] == "pending_analysis"),
        "flagged": sum(1 for i in items if i["status"] == "flagged"),
        "needs_review": sum(1 for i in items if i["status"] == "needs_review"),
        "pending": sum(1 for i in items if i["status"] == "pending"),
        "approved": sum(1 for i in items if i["status"] == "approved"),
    }
    return {"items": items, "counts": counts}


@router.get("/applications/{app_id}")
async def get_application(app_id: str):
    try:
        record = await db.get_application(app_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if not record:
        raise HTTPException(status_code=404, detail="Application not found.")
    return _with_badges(record)


@router.patch("/applications/{app_id}")
async def update_application(app_id: str, body: UpdateApplicationStatusRequest):
    try:
        record = await db.update_application_status(app_id, body.status.value, body.note)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if not record:
        raise HTTPException(status_code=404, detail="Application not found.")
    return _with_badges(record)


@router.delete("/applications/{app_id}", status_code=204)
async def delete_application(app_id: str):
    """Permanently removes a queue record -- distinct from Approve/Flag/
    Reject, which record a reviewer decision but keep the submission around.
    Backs the queue detail view's "Delete" control, for e.g. a test/sample
    submission a reviewer wants out of the queue entirely."""
    try:
        deleted = await db.delete_application(app_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if not deleted:
        raise HTTPException(status_code=404, detail="Application not found.")
    return Response(status_code=204)
