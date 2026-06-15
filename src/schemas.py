from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


Status = Literal["pass", "needs_correction", "cannot_verify"]
ProductType = Literal["distilled_spirits", "wine", "malt_beverage"]


PRODUCT_TYPE_ALIASES = {
    "distilled spirits": "distilled_spirits",
    "distilled-spirit": "distilled_spirits",
    "distilled spirit": "distilled_spirits",
    "spirits": "distilled_spirits",
    "spirit": "distilled_spirits",
    "beer": "malt_beverage",
    "malt beverage": "malt_beverage",
    "malt-beverage": "malt_beverage",
}


class ApplicationData(BaseModel):
    model_config = ConfigDict(extra="allow")

    product_type: ProductType
    brand_name: str
    class_type: str
    alcohol_content: str
    net_contents: str
    name_and_address: Optional[str] = None
    country_of_origin: Optional[str] = None

    @field_validator("product_type", mode="before")
    @classmethod
    def normalize_product_type(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip().lower().replace("/", "_")
            normalized = PRODUCT_TYPE_ALIASES.get(normalized, normalized)
            return normalized
        return value

    @field_validator("brand_name", "class_type", "alcohol_content", "net_contents")
    @classmethod
    def required_text_not_empty(cls, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("must be a string")
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be empty")
        return cleaned

    @field_validator("name_and_address", "country_of_origin", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str):
            cleaned = value.strip()
            return cleaned or None
        return value


class VerificationCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    status: Status
    application_value: Optional[str] = None
    label_value: Optional[str] = None
    evidence_image: Optional[str] = None
    evidence_text: Optional[str] = None
    reason: str

    @field_validator("field", "reason")
    @classmethod
    def required_non_empty_text(cls, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("must be a string")
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be empty")
        return cleaned

    @field_validator("application_value", "label_value", "evidence_image", "evidence_text", mode="before")
    @classmethod
    def blank_optional_text_to_none(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str):
            cleaned = value.strip()
            return cleaned or None
        return str(value)


class VerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_unit: str
    overall_status: Status
    summary: str
    checks: List[VerificationCheck] = Field(min_length=1)

    @field_validator("review_unit", "summary")
    @classmethod
    def non_empty_text(cls, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("must be a string")
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be empty")
        return cleaned


class BatchPreviewRow(BaseModel):
    review_unit: str
    json_status: str
    image_count: int
    validation_status: Literal["ready", "cannot_verify"]
    validation_details: str
