import asyncio
import json
import time

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from app.models.schemas import (
    ApplicationData,
    BatchItemResult,
    BatchVerificationResult,
    VerificationResult,
)
from app.services.extraction import extract_label_fields
from app.services.verification import overall_status, verify_label

router = APIRouter()

# Batch uploads (Sarah/Janet's 200-300 at once scenario) are processed
# concurrently, but capped to avoid hammering the vision API / hitting
# rate limits all at once.
MAX_CONCURRENT_EXTRACTIONS = 6
_semaphore = asyncio.Semaphore(MAX_CONCURRENT_EXTRACTIONS)


async def _run_single_verification(image_bytes: bytes, filename: str, application: ApplicationData) -> VerificationResult:
    start = time.monotonic()
    async with _semaphore:
        extracted, extraction_ms = await asyncio.to_thread(extract_label_fields, image_bytes, filename)
    fields = verify_label(extracted, application)
    total_ms = int((time.monotonic() - start) * 1000)
    return VerificationResult(
        overall_status=overall_status(fields),
        fields=fields,
        extracted=extracted,
        processing_time_ms=total_ms,
        label_filename=filename,
    )


@router.post("/verify", response_model=VerificationResult)
async def verify_single_label(
    label_image: UploadFile = File(...),
    application_data: str = Form(..., description="JSON-encoded ApplicationData"),
):
    try:
        application = ApplicationData(**json.loads(application_data))
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid application_data JSON: {exc}")

    image_bytes = await label_image.read()
    if not image_bytes:
        raise HTTPException(status_code=422, detail="Uploaded label image is empty.")

    try:
        return await _run_single_verification(image_bytes, label_image.filename or "label.jpg", application)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/verify/batch", response_model=BatchVerificationResult)
async def verify_batch(
    label_images: list[UploadFile] = File(...),
    application_data: str = Form(..., description="JSON array of ApplicationData, same length/order as label_images"),
):
    try:
        raw_list = json.loads(application_data)
        if not isinstance(raw_list, list):
            raise ValueError("application_data must be a JSON array")
        applications = [ApplicationData(**item) for item in raw_list]
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid application_data JSON: {exc}")

    if len(applications) != len(label_images):
        raise HTTPException(
            status_code=422,
            detail=f"Mismatch: {len(label_images)} images but {len(applications)} application entries.",
        )

    start = time.monotonic()

    async def _process(index: int, upload: UploadFile, application: ApplicationData) -> BatchItemResult:
        filename = upload.filename or f"label_{index}.jpg"
        try:
            image_bytes = await upload.read()
            if not image_bytes:
                raise RuntimeError("Uploaded label image is empty.")
            result = await _run_single_verification(image_bytes, filename, application)
            return BatchItemResult(index=index, label_filename=filename, result=result)
        except RuntimeError as exc:
            return BatchItemResult(index=index, label_filename=filename, error=str(exc))

    items = await asyncio.gather(
        *[_process(i, upload, app_data) for i, (upload, app_data) in enumerate(zip(label_images, applications))]
    )
    items = sorted(items, key=lambda item: item.index)

    passed = sum(1 for i in items if i.result and i.result.overall_status == "pass")
    failed = sum(1 for i in items if i.result and i.result.overall_status == "fail")
    needs_review = sum(1 for i in items if i.result and i.result.overall_status == "needs_review")
    errored = sum(1 for i in items if i.error)

    return BatchVerificationResult(
        total=len(items),
        passed=passed,
        failed=failed,
        needs_review=needs_review,
        errored=errored,
        items=items,
        processing_time_ms=int((time.monotonic() - start) * 1000),
    )


@router.post("/verify/batch/stream")
async def verify_batch_stream(
    label_images: list[UploadFile] = File(...),
    application_data: str = Form(..., description="JSON array of ApplicationData, same length/order as label_images"),
):
    """Newline-delimited JSON (NDJSON) variant of /verify/batch.

    Sarah's interview described importers dumping 200-300 applications at
    once; watching a spinner for the whole batch with no feedback is the
    same "did this actually work" anxiety that killed the earlier vendor
    pilot. Rather than fake a progress bar client-side, this streams one
    {"type": "progress", ...} line per completed item as results actually
    finish (via asyncio.as_completed, since items run concurrently and do
    not complete in upload order), then a final {"type": "done", ...} line
    with the same payload /verify/batch returns. The plain /verify/batch
    endpoint is kept as-is for simple callers that don't need streaming.
    """
    try:
        raw_list = json.loads(application_data)
        if not isinstance(raw_list, list):
            raise ValueError("application_data must be a JSON array")
        applications = [ApplicationData(**item) for item in raw_list]
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid application_data JSON: {exc}")

    if len(applications) != len(label_images):
        raise HTTPException(
            status_code=422,
            detail=f"Mismatch: {len(label_images)} images but {len(applications)} application entries.",
        )

    # Read all uploads up front -- the underlying SpooledTemporaryFile isn't
    # safe to read concurrently later once we're inside the generator, and
    # we need every image's bytes in hand before kicking off the tasks below.
    read_uploads = []
    for i, upload in enumerate(label_images):
        filename = upload.filename or f"label_{i}.jpg"
        image_bytes = await upload.read()
        read_uploads.append((filename, image_bytes))

    async def _process(index: int, filename: str, image_bytes: bytes, application: ApplicationData) -> BatchItemResult:
        try:
            if not image_bytes:
                raise RuntimeError("Uploaded label image is empty.")
            result = await _run_single_verification(image_bytes, filename, application)
            return BatchItemResult(index=index, label_filename=filename, result=result)
        except RuntimeError as exc:
            return BatchItemResult(index=index, label_filename=filename, error=str(exc))

    async def _generate():
        start = time.monotonic()
        total = len(read_uploads)
        tasks = [
            asyncio.create_task(_process(i, filename, image_bytes, app_data))
            for i, ((filename, image_bytes), app_data) in enumerate(zip(read_uploads, applications))
        ]
        items = []
        completed = 0
        for coro in asyncio.as_completed(tasks):
            item = await coro
            items.append(item)
            completed += 1
            yield json.dumps({
                "type": "progress",
                "completed": completed,
                "total": total,
                "label_filename": item.label_filename,
                "status": item.error and "error" or item.result.overall_status,
            }) + "\n"

        items.sort(key=lambda item: item.index)
        passed = sum(1 for i in items if i.result and i.result.overall_status == "pass")
        failed = sum(1 for i in items if i.result and i.result.overall_status == "fail")
        needs_review = sum(1 for i in items if i.result and i.result.overall_status == "needs_review")
        errored = sum(1 for i in items if i.error)
        final = BatchVerificationResult(
            total=total, passed=passed, failed=failed, needs_review=needs_review, errored=errored,
            items=items, processing_time_ms=int((time.monotonic() - start) * 1000),
        )
        yield json.dumps({"type": "done", "result": json.loads(final.model_dump_json())}) + "\n"

    return StreamingResponse(_generate(), media_type="application/x-ndjson")
