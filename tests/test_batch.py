import json
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from src.batch import invalid_unit_result, parse_batch_zip, preview_rows
from src.config import AppConfig


def png_bytes():
    image = Image.new("RGB", (30, 30), (255, 255, 255))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def application_bytes(extra=None):
    data = {
        "product_type": "distilled_spirits",
        "brand_name": "OLD TOM DISTILLERY",
        "class_type": "Kentucky Straight Bourbon Whiskey",
        "alcohol_content": "45% Alc./Vol. (90 Proof)",
        "net_contents": "750 mL",
    }
    if extra:
        data.update(extra)
    return json.dumps(data).encode("utf-8")


def make_zip(entries):
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return output.getvalue()


def test_valid_batch_zip_folder_parsing():
    data = make_zip(
        {
            "unit-one/application.json": application_bytes(),
            "unit-one/label.png": png_bytes(),
        }
    )
    parsed = parse_batch_zip("batch.zip", data, AppConfig(openai_api_key=None))
    assert len(parsed.units) == 1
    assert parsed.units[0].is_valid
    assert preview_rows(parsed.units)[0].image_count == 1


def test_valid_batch_zip_with_single_wrapper_folder():
    data = make_zip(
        {
            "wrapped-batch/unit-one/application.json": application_bytes(),
            "wrapped-batch/unit-one/label.png": png_bytes(),
            "wrapped-batch/unit-two/application.json": application_bytes(),
            "wrapped-batch/unit-two/label.png": png_bytes(),
        }
    )
    parsed = parse_batch_zip("batch.zip", data, AppConfig(openai_api_key=None))
    assert [unit.display_name for unit in parsed.units] == ["unit-one", "unit-two"]
    assert all(unit.is_valid for unit in parsed.units)


def test_single_direct_product_folder_is_not_treated_as_wrapper():
    data = make_zip(
        {
            "unit-one/application.json": application_bytes(),
            "unit-one/label.png": png_bytes(),
        }
    )
    parsed = parse_batch_zip("batch.zip", data, AppConfig(openai_api_key=None))
    assert [unit.display_name for unit in parsed.units] == ["unit-one"]
    assert parsed.units[0].is_valid


def test_invalid_batch_missing_json():
    data = make_zip({"unit-one/label.png": png_bytes()})
    parsed = parse_batch_zip("batch.zip", data, AppConfig(openai_api_key=None))
    assert not parsed.units[0].is_valid
    assert "Expected exactly one JSON file" in parsed.units[0].validation_details


def test_invalid_batch_multiple_json_files():
    data = make_zip(
        {
            "unit-one/application.json": application_bytes(),
            "unit-one/data.json": application_bytes(),
            "unit-one/label.png": png_bytes(),
        }
    )
    parsed = parse_batch_zip("batch.zip", data, AppConfig(openai_api_key=None))
    assert not parsed.units[0].is_valid
    assert parsed.units[0].json_status == "Found 2"


def test_invalid_batch_unsupported_file_reported():
    data = make_zip(
        {
            "unit-one/application.json": application_bytes(),
            "unit-one/label.png": png_bytes(),
            "unit-one/notes.txt": b"notes",
        }
    )
    parsed = parse_batch_zip("batch.zip", data, AppConfig(openai_api_key=None))
    assert not parsed.units[0].is_valid
    assert "Unsupported file" in parsed.units[0].validation_details


def test_zip_traversal_protection():
    data = make_zip({"../evil/application.json": application_bytes()})
    parsed = parse_batch_zip("batch.zip", data, AppConfig(openai_api_key=None))
    assert parsed.archive_error == "Unsafe ZIP path rejected."
    assert not parsed.units[0].is_valid


def test_corrupt_zip_handled():
    parsed = parse_batch_zip("batch.zip", b"not a zip", AppConfig(openai_api_key=None))
    assert not parsed.units[0].is_valid
    assert "corrupt" in parsed.units[0].validation_details


def test_invalid_preflight_unit_becomes_cannot_verify_result():
    data = make_zip({"unit-one/label.png": png_bytes()})
    parsed = parse_batch_zip("batch.zip", data, AppConfig(openai_api_key=None))
    result = invalid_unit_result(parsed.units[0])
    assert result.overall_status == "cannot_verify"
    assert result.checks[0].field == "review_unit"


def test_generated_real_cola_batch_zip_structure_if_present():
    batch_zip = Path("sample_data/real_cola/real_cola_batch.zip")
    manifest_path = Path("sample_data/real_cola/manifest.json")
    if not batch_zip.exists() or not manifest_path.exists():
        pytest.skip("Real COLA sample data has not been generated locally.")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    parsed = parse_batch_zip(batch_zip.name, batch_zip.read_bytes(), AppConfig(openai_api_key=None))

    expected_units = (
        manifest["passing_units_created"]
        + manifest["mismatch_units_created"]
        + manifest["cannot_verify_units_created"]
    )
    assert parsed.archive_error is None
    assert len(parsed.units) == expected_units

    by_name = {unit.display_name: unit for unit in parsed.units}
    for manifest_unit in manifest["units"]:
        unit_name = Path(manifest_unit["folder"]).name
        unit = by_name[unit_name]
        assert unit.json_file_count == 1
        assert 1 <= unit.supported_image_count <= 10
        if manifest_unit["kind"] in {"passing", "mismatch"}:
            assert unit.is_valid
