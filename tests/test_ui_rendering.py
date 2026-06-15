from src.schemas import VerificationCheck, VerificationResult
from src.ui import (
    _check_card,
    _display_preview_row,
    _preview_table_row,
    _preview_visible_rows,
    _review_card,
    _zip_example_card,
)


def test_batch_review_card_renders_check_cards():
    check = VerificationCheck(
        field="brand_name",
        status="pass",
        application_value="OLD TOM DISTILLERY",
        label_value="OLD TOM DISTILLERY",
        evidence_image="label1.png",
        evidence_text="OLD TOM DISTILLERY",
        reason="The brand name matches.",
    )
    result = VerificationResult(
        review_unit="passing",
        overall_status="pass",
        summary="All checks passed.",
        checks=[check],
    )

    check_html = _check_card(check)
    review_html = _review_card(result)

    assert isinstance(check_html, str)
    assert "Brand name" in check_html
    assert "passing" in review_html
    assert "Brand name" in review_html


def test_preview_row_uses_product_column_and_status_classes():
    ready_row = _display_preview_row(
        {
            "review_unit": "passing",
            "json_status": "Found",
            "image_count": 2,
            "validation_status": "ready",
            "validation_details": "Ready",
        }
    )
    invalid_row = _display_preview_row(
        {
            "review_unit": "missing-field",
            "json_status": "Found",
            "image_count": 1,
            "validation_status": "invalid",
            "validation_details": "Missing net contents.",
        }
    )

    assert "Product" in ready_row
    assert "Review unit" not in ready_row
    assert ready_row["Status"] == "Ready"
    assert invalid_row["Status"] == "Cannot verify"

    headers = ["Product", "JSON", "Images", "Status", "Details"]
    assert "status-ready" in _preview_table_row(ready_row, headers)
    assert "status-warning" in _preview_table_row(invalid_row, headers)


def test_preview_visible_rows_limits_to_ten_until_expanded():
    rows = [{"Product": f"product-{index}"} for index in range(12)]

    assert len(_preview_visible_rows(rows, show_all=False)) == 10
    assert len(_preview_visible_rows(rows, show_all=True)) == 12
    assert len(_preview_visible_rows(rows[:10], show_all=False)) == 10


def test_zip_example_card_renders_file_tree_without_code_block_markup():
    html = _zip_example_card("Format 1", "Product folders directly in the ZIP", "batch.zip\n  product/\n    label.png")

    assert "[object Object]" not in html
    assert "<pre" not in html
    assert "<code" not in html
    assert "batch.zip" in html
    assert "&nbsp;&nbsp;product/" in html
