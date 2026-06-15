from __future__ import annotations

import base64
from dataclasses import dataclass
from io import BytesIO
from typing import List, Sequence

from PIL import Image, ImageDraw, ImageFilter

from src.config import AppConfig, GOVERNMENT_WARNING_TEXT, compact_json
from src.image_utils import PreparedImage, prepare_image
from src.results import (
    MalformedModelOutput,
    cannot_verify_result,
    sanitize_error_message,
    validate_and_normalize_model_result,
)
from src.schemas import ApplicationData, VerificationCheck, VerificationResult


SYSTEM_PROMPT = (
    "You verify alcohol label images against application JSON. Use only the uploaded images as evidence. "
    "The images may be unordered and may show different parts of the same label. Do not invent missing label values. "
    "Return structured JSON only."
)


@dataclass(frozen=True)
class SmokeTestResult:
    ok: bool
    message: str


@dataclass(frozen=True)
class ModelImagePayload:
    filename: str
    note: str
    data_url: str


def build_user_prompt(application: ApplicationData, filenames: Sequence[str]) -> str:
    return (
        "Verify this application JSON against all label images in this review unit.\n\n"
        "Application JSON:\n"
        f"{compact_json(application.model_dump(mode='json', exclude_none=True))}\n\n"
        "Required checks: brand_name, class_type, alcohol_content, net_contents, government_warning. "
        "Also check name_and_address and country_of_origin when they are present and non-empty in the application JSON.\n\n"
        "Real COLA/application data may use regulatory category terms that are not printed verbatim on the label. "
        "For class_type, pass when the label shows a product identity or description that is compatible with the "
        "application class, even if the wording is more specific, abbreviated, pluralized, or uses spelling variants "
        "such as whisky/whiskey. For example, whisky specialties can be supported by label text showing whiskey, "
        "bourbon whiskey, straight bourbon whiskey, or a whiskey finished/flavored/specialty description; table red "
        "wine can be supported by red wine, vinho tinto, or equivalent red-wine wording; table white wine can be "
        "supported by white wine or equivalent white-wine wording. When a "
        "compatible product identity is visible, the class_type status must be pass; do not require the exact "
        "application class string to appear on the label. Use needs_correction only when the visible label identity "
        "contradicts the application class; use cannot_verify when no compatible identity can be read.\n\n"
        "For name_and_address, application values may be a full name/address or only a state, country, or other "
        "jurisdiction from the COLA record. If only a jurisdiction is provided, pass when that jurisdiction appears "
        "in a producer, bottler, importer, distiller, brewed-by, address, origin, or similar responsibility statement. "
        "The same jurisdiction value must appear; a different state or country does not satisfy this check. Do not "
        "require a full street address unless the application value itself provides one.\n\n"
        "Government warning text that must be checked exactly:\n"
        f"{GOVERNMENT_WARNING_TEXT}\n\n"
        "Check that the warning appears in the image set and that the heading is presented as GOVERNMENT WARNING. "
        "Compare the warning words after normalizing ordinary whitespace and line breaks; label wrapping alone is not a mismatch. "
        "Do not substitute, paraphrase, or infer warning text from memory. Mark government_warning as needs_correction only "
        "when readable label words clearly contradict the required warning. If the heading or fragments are visible but the "
        "full warning text is too small or unclear to compare confidently, use cannot_verify rather than needs_correction. "
        "If you can quote the complete required warning text from the label, the government_warning status must be pass. "
        "Do not evaluate font size, boldness, contrast, or character density. Do not use ellipses in evidence_text; use null if the "
        "full supporting text is too long to quote confidently.\n\n"
        "Some uploaded source images may be followed by magnified crops of the same source image. Treat those crops as "
        "evidence from the original filename; cite the original filename in evidence_image.\n\n"
        "Use pass when label evidence matches the application. Use needs_correction when label evidence is present "
        "but mismatches, or a required label element is missing. Use cannot_verify when the field is unreadable, "
        "ambiguous, unsupported, or not confidently visible. Include exact evidence text and the source image filename "
        "where possible. Available image filenames: "
        + ", ".join(filenames)
        + "."
    )


def verify_review_unit(
    application: ApplicationData,
    images: Sequence[PreparedImage],
    review_unit: str,
    config: AppConfig,
) -> VerificationResult:
    if config.mock_openai:
        return mock_verify_review_unit(application, images, review_unit)
    if not config.openai_api_key:
        return cannot_verify_result(
            review_unit,
            "OpenAI API key is not configured. Set OPENAI_API_KEY before running live verification.",
            application=application,
        )

    try:
        result = _request_structured_result(application, images, review_unit, config)
        return validate_and_normalize_model_result(
            result,
            application=application,
            review_unit=review_unit,
            image_filenames=[image.filename for image in images],
        )
    except MalformedModelOutput as exc:
        return cannot_verify_result(
            review_unit,
            "Model response did not match the required schema: " + sanitize_error_message(exc),
            application=application,
        )
    except Exception as exc:
        return cannot_verify_result(
            review_unit,
            "OpenAI/model verification failed: " + sanitize_error_message(exc),
            application=application,
        )


def _request_structured_result(
    application: ApplicationData,
    images: Sequence[PreparedImage],
    review_unit: str,
    config: AppConfig,
) -> VerificationResult:
    from openai import OpenAI

    client = OpenAI(api_key=config.openai_api_key, timeout=config.openai_timeout_seconds)
    content: List[dict] = [{"type": "input_text", "text": build_user_prompt(application, [image.filename for image in images])}]
    for payload in _model_image_payloads(images):
        content.append({"type": "input_text", "text": f"{payload.note}: {payload.filename}"})
        content.append({"type": "input_image", "image_url": payload.data_url})

    response = client.responses.parse(
        model=config.openai_model,
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        text_format=VerificationResult,
    )
    if response.output_parsed is None:
        raise MalformedModelOutput("No parsed structured output was returned.")
    return response.output_parsed


def _model_image_payloads(images: Sequence[PreparedImage]) -> List[ModelImagePayload]:
    payloads: List[ModelImagePayload] = []
    for image in images:
        payloads.append(ModelImagePayload(filename=image.filename, note="Source image filename", data_url=image.data_url()))
        payloads.extend(_wide_label_crop_payloads(image))
    return payloads


def _wide_label_crop_payloads(image: PreparedImage) -> List[ModelImagePayload]:
    try:
        with Image.open(BytesIO(image.api_bytes)) as source:
            source.load()
            source = source.convert("RGB")
    except OSError:
        return []

    width, height = source.size
    if height <= 0 or width / height < 2.2 or width < 900:
        return []

    payloads: List[ModelImagePayload] = []
    crop_count = 3
    overlap_px = round(width * 0.04)
    for index in range(crop_count):
        left = max(0, round(index * width / crop_count) - overlap_px)
        right = min(width, round((index + 1) * width / crop_count) + overlap_px)
        if right <= left:
            continue
        payloads.append(
            ModelImagePayload(
                filename=image.filename,
                note=f"Magnified crop {index + 1} of {crop_count} from source image filename",
                data_url=_crop_data_url(source, (left, 0, right, height)),
            )
        )
        lower_top = round(height * 0.25)
        lower_bottom = round(height * 0.96)
        payloads.append(
            ModelImagePayload(
                filename=image.filename,
                note=f"Magnified lower-detail crop {index + 1} of {crop_count} from source image filename",
                data_url=_crop_data_url(source, (left, lower_top, right, lower_bottom)),
            )
        )

    fine_count = 5
    fine_overlap_px = round(width * 0.025)
    fine_top = round(height * 0.28)
    fine_bottom = round(height * 0.92)
    for index in range(fine_count):
        left = max(0, round(index * width / fine_count) - fine_overlap_px)
        right = min(width, round((index + 1) * width / fine_count) + fine_overlap_px)
        if right <= left:
            continue
        payloads.append(
            ModelImagePayload(
                filename=image.filename,
                note=f"Magnified fine-detail lower crop {index + 1} of {fine_count} from source image filename",
                data_url=_crop_data_url(source, (left, fine_top, right, fine_bottom)),
            )
        )
    return payloads


def _crop_data_url(source: Image.Image, box: tuple[int, int, int, int]) -> str:
    crop = source.crop(box)
    target_width = 1600
    target_height = max(1, round(crop.height * (target_width / crop.width)))
    crop = crop.resize((target_width, target_height), Image.Resampling.LANCZOS)
    crop = crop.filter(ImageFilter.UnsharpMask(radius=1.0, percent=150, threshold=2))
    output = BytesIO()
    crop.save(output, format="JPEG", quality=86, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")


def mock_verify_review_unit(
    application: ApplicationData,
    images: Sequence[PreparedImage],
    review_unit: str,
) -> VerificationResult:
    filename = images[0].filename if images else None
    checks = [
        VerificationCheck(
            field="brand_name",
            status="pass",
            application_value=application.brand_name,
            label_value=application.brand_name,
            evidence_image=filename,
            evidence_text=application.brand_name,
            reason="Mock mode assumes the brand name is visible and matches.",
        ),
        VerificationCheck(
            field="class_type",
            status="pass",
            application_value=application.class_type,
            label_value=application.class_type,
            evidence_image=filename,
            evidence_text=application.class_type,
            reason="Mock mode assumes the class/type is visible and matches.",
        ),
        VerificationCheck(
            field="alcohol_content",
            status="pass",
            application_value=application.alcohol_content,
            label_value=application.alcohol_content,
            evidence_image=filename,
            evidence_text=application.alcohol_content,
            reason="Mock mode assumes the alcohol content is visible and matches.",
        ),
        VerificationCheck(
            field="net_contents",
            status="pass",
            application_value=application.net_contents,
            label_value=application.net_contents,
            evidence_image=filename,
            evidence_text=application.net_contents,
            reason="Mock mode assumes the net contents are visible and match.",
        ),
        VerificationCheck(
            field="government_warning",
            status="cannot_verify",
            application_value=None,
            label_value=None,
            evidence_image=None,
            evidence_text=None,
            reason="Mock mode does not inspect uploaded image text.",
        ),
    ]
    if application.name_and_address:
        checks.append(
            VerificationCheck(
                field="name_and_address",
                status="pass",
                application_value=application.name_and_address,
                label_value=application.name_and_address,
                evidence_image=filename,
                evidence_text=application.name_and_address,
                reason="Mock mode assumes the name/address is visible and matches.",
            )
        )
    if application.country_of_origin:
        checks.append(
            VerificationCheck(
                field="country_of_origin",
                status="pass",
                application_value=application.country_of_origin,
                label_value=application.country_of_origin,
                evidence_image=filename,
                evidence_text=application.country_of_origin,
                reason="Mock mode assumes the country of origin is visible and matches.",
            )
        )
    return VerificationResult(
        review_unit=review_unit,
        overall_status="cannot_verify",
        summary="Mock verification completed. Live OpenAI image reading was not used.",
        checks=checks,
    )


def smoke_test_model(config: AppConfig) -> SmokeTestResult:
    if config.mock_openai:
        return SmokeTestResult(ok=True, message="Mock mode is enabled; live OpenAI smoke test was skipped.")
    if not config.openai_api_key:
        return SmokeTestResult(ok=False, message="OPENAI_API_KEY is not configured.")
    application = ApplicationData(
        product_type="distilled_spirits",
        brand_name="SMOKE TEST BRAND",
        class_type="Straight Bourbon Whiskey",
        alcohol_content="45% Alc./Vol. (90 Proof)",
        net_contents="750 mL",
        name_and_address="Bottled by Smoke Test Distilling, Louisville, KY",
        country_of_origin=None,
    )
    images = _smoke_test_images(config)
    try:
        result = _request_structured_result(application, images, "openai-smoke-test", config)
        validate_and_normalize_model_result(
            result,
            application=application,
            review_unit="openai-smoke-test",
            image_filenames=[image.filename for image in images],
        )
        return SmokeTestResult(ok=True, message=f"{config.openai_model} accepted multiple images and returned schema output.")
    except Exception as exc:
        return SmokeTestResult(ok=False, message="OpenAI smoke test failed: " + sanitize_error_message(exc))


def _smoke_test_images(config: AppConfig) -> List[PreparedImage]:
    image1 = _label_image_bytes(
        [
            "SMOKE TEST BRAND",
            "Straight Bourbon Whiskey",
            "45% Alc./Vol. (90 Proof)",
            "750 mL",
        ]
    )
    image2 = _label_image_bytes(
        [
            "Bottled by Smoke Test Distilling, Louisville, KY",
            "GOVERNMENT WARNING: (1) According to the Surgeon General, women should not",
            "drink alcoholic beverages during pregnancy because of the risk of birth defects.",
            "(2) Consumption of alcoholic beverages impairs your ability to drive a car or",
            "operate machinery, and may cause health problems.",
        ],
        size=(1200, 760),
    )
    return [
        prepare_image("smoke-front.png", image1, "image/png", config.max_image_mb, config.image_max_side_px, config.image_jpeg_quality),
        prepare_image("smoke-back.png", image2, "image/png", config.max_image_mb, config.image_max_side_px, config.image_jpeg_quality),
    ]


def _label_image_bytes(lines: Sequence[str], size: tuple = (1000, 700)) -> bytes:
    image = Image.new("RGB", size, (250, 250, 246))
    draw = ImageDraw.Draw(image)
    y = 60
    for line in lines:
        draw.text((60, y), line, fill=(20, 20, 20))
        y += 72
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()
