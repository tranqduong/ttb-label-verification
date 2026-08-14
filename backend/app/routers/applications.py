"""
Application Queue endpoints.

/verify (in verify.py) is stateless — it processes one upload and returns
the result, nothing is kept. This router adds a persisted counterpart:
every submission through Single Label Review is written to the
applications table (see app/services/db.py) so it shows up in the
Application Queue with a submitted date, and a reviewer can Approve/Flag/
Reject it from either the inline result or the queue later.

Scope note: only Single Label Review submissions are queued. Batch Upload
stays the stateless concurrent-triage tool it already was — queuing 300
batch items would need pagination/bulk-action UI that's out of scope for
this pass, and batch's own CSV export already covers "get all these
results out."
"""
import asyncio
import json

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

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
        "created_at": record["created_at"],
        "status": record["status"],
        "brand_name": app_data.get("brand_name"),
        "class_type": app_data.get("class_type"),
        "details": " · ".join(p for p in details_parts if p),
        "net_contents": app_data.get("net_contents"),
        "is_import": app_data.get("is_import", False),
        "overall_status": record["overall_status"],
    }


@router.post("/applications/verify")
async def verify_and_queue(
    label_image: UploadFile = File(...),
    application_data: str = Form(..., description="JSON-encoded ApplicationData"),
):
    """Same extraction + comparison as POST /verify, but persists the
    result into the Application Queue instead of returning it once and
    forgetting it. Every submission starts as needs_review — see
    ApplicationStatus in schemas.py for why — the reviewer workflow
    (Approve/Flag/Reject) is what moves it from there."""
    try:
        application = ApplicationData(**json.loads(application_data))
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid application_data JSON: {exc}")

    image_bytes = await label_image.read()
    if not image_bytes:
        raise HTTPException(status_code=422, detail="Uploaded label image is empty.")

    filename = label_image.filename or "label.jpg"
    try:
        extracted, _elapsed_ms = await asyncio.to_thread(extract_label_fields, image_bytes, filename)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    fields = verify_label(extracted, application)
    status = overall_status(fields)

    try:
        record = await db.insert_application(
            application_data=application.model_dump(mode="json"),
            extracted=extracted.model_dump(mode="json"),
            field_results=[f.model_dump(mode="json") for f in fields],
            overall_status=status,
            label_filename=filename,
        )
    except RuntimeError as exc:
        # No Postgres storage provisioned yet -- see db.py's error message.
        raise HTTPException(status_code=503, detail=str(exc))

    return _with_badges(record)


@router.get("/applications")
async def list_applications():
    try:
        records = await db.list_applications()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    items = [_summarize(r) for r in records]
    counts = {
        "all": len(items),
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
