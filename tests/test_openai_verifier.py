from src.config import AppConfig
from src.image_utils import prepare_image
from src.openai_verifier import _model_image_payloads, build_user_prompt, smoke_test_model, verify_review_unit
from src.schemas import ApplicationData, VerificationCheck, VerificationResult


def app_data():
    return ApplicationData(
        product_type="distilled_spirits",
        brand_name="SMOKE TEST BRAND",
        class_type="Straight Bourbon Whiskey",
        alcohol_content="45% Alc./Vol. (90 Proof)",
        net_contents="750 mL",
        name_and_address="Bottled by Smoke Test Distilling, Louisville, KY",
    )


def valid_result():
    checks = []
    for field in ["brand_name", "class_type", "alcohol_content", "net_contents", "government_warning", "name_and_address"]:
        checks.append(
            VerificationCheck(
                field=field,
                status="pass",
                application_value=None,
                label_value="x",
                evidence_image="smoke-front.png",
                evidence_text="x",
                reason="ok",
            )
        )
    return VerificationResult(review_unit="unit", overall_status="pass", summary="ok", checks=checks)


def tiny_png():
    from io import BytesIO

    from PIL import Image

    image = Image.new("RGB", (20, 20), (255, 255, 255))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_smoke_test_success_with_mocked_request(monkeypatch):
    monkeypatch.setattr("src.openai_verifier._request_structured_result", lambda *args, **kwargs: valid_result())
    result = smoke_test_model(AppConfig(openai_api_key="test-key"))
    assert result.ok


def test_prompt_guides_real_cola_class_and_jurisdiction_matching():
    prompt = build_user_prompt(
        ApplicationData(
            product_type="distilled_spirits",
            brand_name="Arthur Wheeler Spirits Company",
            class_type="whisky specialties",
            alcohol_content="50.5",
            net_contents="750.0 milliliters",
            name_and_address="maryland",
        ),
        ["label_1.jpg"],
    )
    assert "regulatory category terms" in prompt
    assert "whisky specialties can be supported" in prompt
    assert "table red wine can be supported by red wine" in prompt
    assert "class_type status must be pass" in prompt
    assert "only a state, country, or other jurisdiction" in prompt
    assert "The same jurisdiction value must appear" in prompt
    assert "Do not substitute, paraphrase, or infer warning text from memory" in prompt
    assert "complete required warning text" in prompt


def test_wide_label_payloads_include_magnified_crops():
    from io import BytesIO

    from PIL import Image

    image = Image.new("RGB", (800, 297), (255, 255, 255))
    output = BytesIO()
    image.save(output, format="JPEG")

    prepared = prepare_image("wide-label.jpg", output.getvalue(), "image/jpeg")
    payloads = _model_image_payloads([prepared])

    assert len(payloads) == 12
    assert {payload.filename for payload in payloads} == {"wide-label.jpg"}
    assert [payload.note for payload in payloads if "Magnified crop" in payload.note]
    assert [payload.note for payload in payloads if "lower-detail" in payload.note]
    assert [payload.note for payload in payloads if "fine-detail" in payload.note]


def test_smoke_test_failure_with_mocked_request(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr("src.openai_verifier._request_structured_result", fail)
    result = smoke_test_model(AppConfig(openai_api_key="test-key"))
    assert not result.ok
    assert "model unavailable" in result.message


def test_verify_review_unit_malformed_model_output_becomes_cannot_verify(monkeypatch):
    bad = VerificationResult(
        review_unit="unit",
        overall_status="pass",
        summary="bad",
        checks=[
            VerificationCheck(
                field="brand_name",
                status="pass",
                application_value=None,
                label_value="x",
                evidence_image="label.png",
                evidence_text="x",
                reason="ok",
            )
        ],
    )
    monkeypatch.setattr("src.openai_verifier._request_structured_result", lambda *args, **kwargs: bad)
    image = prepare_image("label.png", tiny_png(), "image/png")
    result = verify_review_unit(app_data(), [image], "unit", AppConfig(openai_api_key="test-key"))
    assert result.overall_status == "cannot_verify"
    assert "missing required" in result.summary


def test_verify_review_unit_api_error_becomes_cannot_verify(monkeypatch):
    def fail(*args, **kwargs):
        raise TimeoutError("request timed out")

    monkeypatch.setattr("src.openai_verifier._request_structured_result", fail)
    image = prepare_image("label.png", tiny_png(), "image/png")
    result = verify_review_unit(app_data(), [image], "unit", AppConfig(openai_api_key="test-key"))
    assert result.overall_status == "cannot_verify"
    assert "request timed out" in result.summary
