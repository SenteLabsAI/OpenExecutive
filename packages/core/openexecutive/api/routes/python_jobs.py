"""Result files of Python jobs (workflows/python_job.py), for the principal.

  GET /python-jobs/{job_id}/{name} — download one file a job wrote

The Executive links these in its reply. They are the principal's alone, like
the jobs themselves (``run_python_job`` is offered only on their own turns),
and served as downloads that a browser never renders in the app's origin.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

router = APIRouter()


@router.get("/python-jobs/{job_id}/{name}")
def download_job_file(job_id: str, name: str, request: Request) -> FileResponse:
    from openexecutive.api.routes.people import caller_is_principal
    from openexecutive.workflows.python_job import result_file

    if not caller_is_principal(request):
        raise HTTPException(status_code=403, detail="Only the account owner can download these.")
    path = result_file(job_id, name)
    if path is None:
        raise HTTPException(status_code=404, detail="No such file, or it has expired.")
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/octet-stream",
        headers={"Content-Security-Policy": "sandbox", "X-Content-Type-Options": "nosniff"},
    )
