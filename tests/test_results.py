import pytest

from src.config import GOVERNMENT_WARNING_TEXT
from src.results import MalformedModelOutput, compute_overall_status, summary_counts, validate_and_normalize_model_result
from src.schemas import ApplicationData, VerificationCheck, VerificationResult


def app_data():
    return ApplicationData(
        product_type="distilled_spirits",
        brand_name="OLD TOM DISTILLERY",
        class_type="Bourbon Whiskey",
        alcohol_content="45% Alc./Vol.",
        net_contents="750 mL",
    )


def check(field, status="pass"):
    return VerificationCheck(
        field=field,
        status=status,
        application_value="x",
        label_value="x" if status == "pass" else None,
        evidence_image="label.png",
        evidence_text="x",
        reason="reason",
    )


def full_result(status="pass"):
    checks = [
        check("brand_name", status),
        check("class_type"),
        check("alcohol_content"),
        check("net_contents"),
        check("government_warning"),
    ]
    return VerificationResult(review_unit="unit", overall_status="pass", summary="summary", checks=checks)


def test_status_aggregation_needs_correction_wins():
    checks = [check("brand_name", "cannot_verify"), check("class_type", "needs_correction")]
    assert compute_overall_status(checks) == "needs_correction"


def test_status_aggregation_cannot_verify_next():
    checks = [check("brand_name", "pass"), check("class_type", "cannot_verify")]
    assert compute_overall_status(checks) == "cannot_verify"


def test_status_aggregation_pass():
    checks = [check("brand_name", "pass"), check("class_type", "pass")]
    assert compute_overall_status(checks) == "pass"


def test_normalize_computes_overall_status():
    result = full_result(status="needs_correction")
    normalized = validate_and_normalize_model_result(result, app_data(), "unit", ["label.png"])
    assert normalized.overall_status == "needs_correction"
    assert normalized.checks[0].application_value == "OLD TOM DISTILLERY"


def test_malformed_model_output_missing_check():
    result = VerificationResult(
        review_unit="unit",
        overall_status="pass",
        summary="summary",
        checks=[check("brand_name")],
    )
    with pytest.raises(MalformedModelOutput, match="missing required"):
        validate_and_normalize_model_result(result, app_data(), "unit", ["label.png"])


def test_summary_counts():
    results = [
        full_result("pass"),
        VerificationResult(review_unit="two", overall_status="needs_correction", summary="s", checks=[check("brand_name")]),
        VerificationResult(review_unit="three", overall_status="cannot_verify", summary="s", checks=[check("brand_name")]),
    ]
    counts = summary_counts(results)
    assert counts == {"pass": 1, "needs_correction": 1, "cannot_verify": 1}


def test_location_check_normalizes_to_pass_when_evidence_contains_application_value():
    application = ApplicationData(
        product_type="distilled_spirits",
        brand_name="A",
        class_type="Bourbon Whiskey",
        alcohol_content="45%",
        net_contents="750 mL",
        name_and_address="maryland",
    )
    result = VerificationResult(
        review_unit="unit",
        overall_status="needs_correction",
        summary="summary",
        checks=[
            check("brand_name"),
            check("class_type"),
            check("alcohol_content"),
            check("net_contents"),
            check("government_warning"),
            VerificationCheck(
                field="name_and_address",
                status="needs_correction",
                application_value=None,
                label_value="UPPER MARLBORO, MARYLAND",
                evidence_image="label.png",
                evidence_text="UPPER MARLBORO, MARYLAND",
                reason="underspecified",
            ),
        ],
    )

    normalized = validate_and_normalize_model_result(result, application, "unit", ["label.png"])

    assert normalized.overall_status == "pass"
    assert normalized.checks[-1].status == "pass"


def test_unreadable_government_warning_is_cannot_verify_not_needs_correction():
    result = full_result()
    result.checks[4] = VerificationCheck(
        field="government_warning",
        status="needs_correction",
        application_value=None,
        label_value=None,
        evidence_image="label.png",
        evidence_text="GOVERNMENT WARNING heading visible",
        reason="The warning body is too blurry to read.",
    )

    normalized = validate_and_normalize_model_result(result, app_data(), "unit", ["label.png"])

    assert normalized.overall_status == "cannot_verify"
    assert normalized.checks[4].status == "cannot_verify"


def test_government_warning_normalizes_to_pass_when_evidence_contains_required_text():
    result = full_result()
    result.checks[4] = VerificationCheck(
        field="government_warning",
        status="cannot_verify",
        application_value=None,
        label_value=None,
        evidence_image="label.png",
        evidence_text=GOVERNMENT_WARNING_TEXT,
        reason="not confident",
    )

    normalized = validate_and_normalize_model_result(result, app_data(), "unit", ["label.png"])

    assert normalized.overall_status == "pass"
    assert normalized.checks[4].status == "pass"
