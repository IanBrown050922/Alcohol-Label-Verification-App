from __future__ import annotations

import json
import re
from typing import Dict, Iterable, List, Optional, Sequence

from src.config import GOVERNMENT_WARNING_TEXT
from src.schemas import ApplicationData, VerificationCheck, VerificationResult


BASE_CHECK_FIELDS = ["brand_name", "class_type", "alcohol_content", "net_contents", "government_warning"]


class MalformedModelOutput(ValueError):
    pass


def expected_check_fields(application: Optional[ApplicationData]) -> List[str]:
    fields = list(BASE_CHECK_FIELDS)
    if application is not None:
        if application.name_and_address:
            fields.append("name_and_address")
        if application.country_of_origin:
            fields.append("country_of_origin")
    return fields


def application_value_for_field(application: Optional[ApplicationData], field: str) -> Optional[str]:
    if application is None:
        return None
    if field == "government_warning":
        return None
    value = getattr(application, field, None)
    if value is None:
        return None
    return str(value)


def compute_overall_status(checks: Sequence[VerificationCheck], status_fields: Optional[Iterable[str]] = None) -> str:
    fields = set(status_fields) if status_fields is not None else {check.field for check in checks}
    relevant = [check for check in checks if check.field in fields]
    if any(check.status == "needs_correction" for check in relevant):
        return "needs_correction"
    if any(check.status == "cannot_verify" for check in relevant):
        return "cannot_verify"
    return "pass"


def validate_and_normalize_model_result(
    result: VerificationResult,
    application: ApplicationData,
    review_unit: str,
    image_filenames: Optional[Iterable[str]] = None,
) -> VerificationResult:
    expected = expected_check_fields(application)
    by_field: Dict[str, VerificationCheck] = {}
    for check in result.checks:
        if check.field in by_field:
            raise MalformedModelOutput(f"Model returned duplicate check for field '{check.field}'.")
        by_field[check.field] = check

    missing = [field for field in expected if field not in by_field]
    if missing:
        raise MalformedModelOutput("Model response is missing required check(s): " + ", ".join(missing) + ".")

    valid_images = set(image_filenames or [])
    normalized_checks = []
    for field in expected:
        check = by_field[field]
        evidence_image = check.evidence_image
        reason = check.reason
        if evidence_image and valid_images and evidence_image not in valid_images:
            evidence_image = None
            reason = reason + " The model cited an unknown source image, so the source was cleared."
        normalized_checks.append(
            _normalize_self_contradictory_check(
                check.model_copy(
                    update={
                        "application_value": application_value_for_field(application, field),
                        "evidence_image": evidence_image,
                        "reason": reason,
                    }
                ),
                field,
            )
        )

    overall = compute_overall_status(normalized_checks, expected)
    summary = result.summary.strip()
    return VerificationResult(
        review_unit=review_unit,
        overall_status=overall,  # type: ignore[arg-type]
        summary=summary,
        checks=normalized_checks,
    )


def _normalize_self_contradictory_check(check: VerificationCheck, field: str) -> VerificationCheck:
    if field in {"name_and_address", "country_of_origin"} and check.application_value:
        evidence = _normalized_text(" ".join(value or "" for value in [check.label_value, check.evidence_text]))
        application_value = _normalized_text(check.application_value)
        if application_value and application_value in evidence and check.status != "pass":
            return check.model_copy(
                update={
                    "status": "pass",
                    "reason": check.reason
                    + " The cited evidence contains the application location value, so this check was normalized to pass.",
                }
            )

    if field == "government_warning":
        evidence = _normalized_text(check.evidence_text or "")
        required = _normalized_text(GOVERNMENT_WARNING_TEXT)
        if required and required in evidence and check.status != "pass":
            return check.model_copy(
                update={
                    "status": "pass",
                    "reason": check.reason
                    + " The cited evidence contains the required warning text, so this check was normalized to pass.",
                }
            )
        if check.status == "needs_correction" and _is_unreadable_warning_reason(check.reason):
            return check.model_copy(
                update={
                    "status": "cannot_verify",
                    "reason": check.reason
                    + " Unreadable warning text is cannot_verify rather than needs_correction.",
                }
            )

    return check


def _normalized_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _is_unreadable_warning_reason(reason: str) -> bool:
    normalized = _normalized_text(reason)
    return any(
        marker in normalized
        for marker in [
            "not readable",
            "not legible",
            "too blurry",
            "too small",
            "unclear",
            "cannot be read",
            "cannot confidently",
            "cannot verify",
            "cannot be verified",
        ]
    )


def cannot_verify_result(
    review_unit: str,
    reason: str,
    application: Optional[ApplicationData] = None,
    fields: Optional[Sequence[str]] = None,
    summary: Optional[str] = None,
) -> VerificationResult:
    check_fields = list(fields) if fields is not None else expected_check_fields(application)
    if not check_fields:
        check_fields = ["review_unit"]
    checks = [
        VerificationCheck(
            field=field,
            status="cannot_verify",
            application_value=application_value_for_field(application, field),
            label_value=None,
            evidence_image=None,
            evidence_text=None,
            reason=reason,
        )
        for field in check_fields
    ]
    return VerificationResult(
        review_unit=review_unit,
        overall_status="cannot_verify",
        summary=summary or reason,
        checks=checks,
    )


def sanitize_error_message(error: object, max_length: int = 500) -> str:
    message = str(error) or error.__class__.__name__
    message = re.sub(r"sk-[A-Za-z0-9_\-]+", "[redacted-api-key]", message)
    message = re.sub(r"\s+", " ", message).strip()
    if len(message) > max_length:
        message = message[: max_length - 3] + "..."
    return message


def summary_counts(results: Sequence[VerificationResult]) -> Dict[str, int]:
    counts = {"pass": 0, "needs_correction": 0, "cannot_verify": 0}
    for result in results:
        counts[result.overall_status] += 1
    return counts


def result_to_plain_dict(result: VerificationResult) -> dict:
    return result.model_dump(mode="json")


def results_to_json(results: Sequence[VerificationResult]) -> str:
    return json.dumps([result_to_plain_dict(result) for result in results], indent=2, ensure_ascii=False)


def single_result_to_json(result: VerificationResult) -> str:
    return json.dumps(result_to_plain_dict(result), indent=2, ensure_ascii=False)
