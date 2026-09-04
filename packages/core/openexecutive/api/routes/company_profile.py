from __future__ import annotations

from fastapi import APIRouter, HTTPException

from openexecutive.api.models import (
    CompanyProfilePutRequest,
    CompanyProfileResponse,
    CompanyProfileUpdateRequest,
)
from openexecutive.config import get_settings
from openexecutive.memory.company_profile import CompanyProfile
from openexecutive.onboarding.profile_builder import load_or_create_profile

router = APIRouter()


@router.get("/company-profile", response_model=CompanyProfileResponse)
async def get_company_profile() -> CompanyProfileResponse:
    profile = load_or_create_profile()
    if profile.is_empty():
        raise HTTPException(status_code=404, detail="No company profile found. Complete onboarding first.")
    return CompanyProfileResponse(**profile.model_dump())


@router.patch("/company-profile", response_model=CompanyProfileResponse)
async def update_company_profile(body: CompanyProfileUpdateRequest) -> CompanyProfileResponse:
    settings = get_settings()
    profile = load_or_create_profile()
    if profile.is_empty():
        raise HTTPException(status_code=404, detail="No company profile found. Complete onboarding first.")

    update_data = body.model_dump(exclude_unset=True)
    # Convert nested Pydantic models to dicts so model_copy merges cleanly
    update_data = {
        k: v.model_dump() if hasattr(v, "model_dump") else v
        for k, v in update_data.items()
    }

    updated = profile.model_copy(update=update_data)
    validated = CompanyProfile.model_validate(updated.model_dump())
    validated.save_to_yaml(settings.company_profile_path)

    return CompanyProfileResponse(**validated.model_dump())


@router.put("/company-profile", response_model=CompanyProfileResponse)
async def put_company_profile(body: CompanyProfilePutRequest) -> CompanyProfileResponse:
    """Create or fully replace the company profile.

    Unlike PATCH, this works on a fresh install (no wizard run) and does not
    merge: sections omitted from the body reset to their empty defaults. It is
    the path `openexecutive seed-org` uses to configure a real organization.
    """
    settings = get_settings()
    validated = CompanyProfile.model_validate(body.model_dump(exclude_none=True))
    validated.save_to_yaml(settings.company_profile_path)
    return CompanyProfileResponse(**validated.model_dump())
