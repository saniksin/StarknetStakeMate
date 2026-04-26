"""``/api/v1/locales/{lang}`` — locale bundles for the Mini App.

The bot already ships 8 hand-translated JSON bundles in ``locales/`` for
its own UI; rather than duplicating them in the webapp we expose them
through the API so the Mini App can reuse the same translations and any
update is instantly visible without a webapp redeploy.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Path, status

from data.languages import locales as _LOCALES

router = APIRouter(prefix="/api/v1/locales", tags=["locales"])

_SUPPORTED = ("en", "ru", "ua", "zh", "ko", "es", "de", "pl")


@router.get("/{lang}", summary="Return the full key/value locale bundle")
async def get_locale(
    lang: str = Path(..., description="Two-letter locale prefix"),
) -> dict[str, str]:
    """Return the bundle for ``lang``.

    Falls back to ``en`` when the requested locale isn't shipped (rather
    than 404'ing) — keeps the Mini App rendering instead of breaking.
    """
    if lang not in _SUPPORTED:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"unsupported locale '{lang}', expected one of {list(_SUPPORTED)}",
        )
    bundle = _LOCALES.get(lang) or _LOCALES.get("en") or {}
    return bundle


@router.get("", summary="List supported locales")
async def list_supported() -> dict[str, list[str]]:
    """Cheap discovery endpoint for the language picker."""
    available = [code for code in _SUPPORTED if code in _LOCALES]
    return {"available": available}
