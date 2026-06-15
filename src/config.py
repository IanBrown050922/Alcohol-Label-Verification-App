from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

try:
    from dotenv import load_dotenv

    load_dotenv(".env", override=False)
    load_dotenv("local.env", override=False)
except Exception:
    pass


DEFAULT_MODEL = "gpt-5.4-nano"
MODEL_FALLBACK = "gpt-5.4-mini"

ALLOWED_PRODUCT_TYPES = {"distilled_spirits", "wine", "malt_beverage"}
REQUIRED_APPLICATION_FIELDS = (
    "product_type",
    "brand_name",
    "class_type",
    "alcohol_content",
    "net_contents",
)
OPTIONAL_APPLICATION_FIELDS = ("name_and_address", "country_of_origin")
SUPPORTED_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".jpe", ".png")

GOVERNMENT_WARNING_TEXT = (
    "GOVERNMENT WARNING: (1) According to the Surgeon General, women should not "
    "drink alcoholic beverages during pregnancy because of the risk of birth "
    "defects. (2) Consumption of alcoholic beverages impairs your ability to "
    "drive a car or operate machinery, and may cause health problems."
)

NO_STORAGE_NOTICE = (
    "Files are processed for this verification run only and are not permanently stored by the app."
)

DEFAULT_APPLICATION_TEMPLATE = {
    "product_type": "distilled_spirits",
    "brand_name": "OLD TOM DISTILLERY",
    "class_type": "Kentucky Straight Bourbon Whiskey",
    "alcohol_content": "45% Alc./Vol. (90 Proof)",
    "net_contents": "750 mL",
    "name_and_address": "Bottled by Old Tom Distillery, Frankfort, KY",
    "country_of_origin": None,
}


@dataclass(frozen=True)
class AppConfig:
    openai_api_key: Optional[str]
    openai_model: str = DEFAULT_MODEL
    openai_timeout_seconds: int = 30
    max_images_per_unit: int = 10
    max_image_mb: int = 10
    max_zip_mb: int = 100
    max_batch_units: int = 50
    image_max_side_px: int = 1600
    image_jpeg_quality: int = 82
    mock_openai: bool = False

    @property
    def has_openai_key(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def can_call_openai(self) -> bool:
        return self.mock_openai or self.has_openai_key


def default_application_json() -> str:
    return json.dumps(DEFAULT_APPLICATION_TEMPLATE, indent=2)


def _streamlit_secret(name: str) -> Optional[str]:
    local_secrets = Path.cwd() / ".streamlit" / "secrets.toml"
    global_secrets = Path.home() / ".streamlit" / "secrets.toml"
    if not local_secrets.exists() and not global_secrets.exists():
        return None
    try:
        import streamlit as st

        value = st.secrets.get(name)
        if value is None:
            return None
        return str(value)
    except Exception:
        return None


def _setting(name: str, default: Optional[str] = None, use_streamlit_secrets: bool = False) -> Optional[str]:
    value = os.getenv(name)
    if value is not None:
        return value
    if use_streamlit_secrets:
        value = _streamlit_secret(name)
        if value is not None:
            return value
    return default


def _int_setting(name: str, default: int, use_streamlit_secrets: bool = False) -> int:
    value = _setting(name, None, use_streamlit_secrets)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _bool_setting(name: str, default: bool = False, use_streamlit_secrets: bool = False) -> bool:
    value = _setting(name, None, use_streamlit_secrets)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_config(use_streamlit_secrets: bool = False) -> AppConfig:
    return AppConfig(
        openai_api_key=_setting("OPENAI_API_KEY", None, use_streamlit_secrets),
        openai_model=_setting("OPENAI_MODEL", DEFAULT_MODEL, use_streamlit_secrets) or DEFAULT_MODEL,
        openai_timeout_seconds=_int_setting("OPENAI_TIMEOUT_SECONDS", 30, use_streamlit_secrets),
        max_images_per_unit=_int_setting("MAX_IMAGES_PER_UNIT", 10, use_streamlit_secrets),
        max_image_mb=_int_setting("MAX_IMAGE_MB", 10, use_streamlit_secrets),
        max_zip_mb=_int_setting("MAX_ZIP_MB", 100, use_streamlit_secrets),
        max_batch_units=_int_setting("MAX_BATCH_UNITS", 50, use_streamlit_secrets),
        image_max_side_px=_int_setting("IMAGE_MAX_SIDE_PX", 1600, use_streamlit_secrets),
        image_jpeg_quality=_int_setting("IMAGE_JPEG_QUALITY", 82, use_streamlit_secrets),
        mock_openai=_bool_setting("ALV_MOCK_OPENAI", False, use_streamlit_secrets),
    )


def compact_json(data: Any) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)
