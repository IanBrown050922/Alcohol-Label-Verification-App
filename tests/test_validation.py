import pytest

from src.config import AppConfig
from src.image_utils import ImageValidationError, is_supported_image_extension, prepare_image_collection
from src.validation import InputValidationError, parse_application_text


VALID_JSON = """
{
  "product_type": "distilled_spirits",
  "brand_name": "OLD TOM DISTILLERY",
  "class_type": "Kentucky Straight Bourbon Whiskey",
  "alcohol_content": "45% Alc./Vol. (90 Proof)",
  "net_contents": "750 mL",
  "name_and_address": "",
  "country_of_origin": null
}
"""


def png_bytes():
    from io import BytesIO

    from PIL import Image

    image = Image.new("RGB", (20, 20), (255, 255, 255))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def image_size(data):
    from io import BytesIO

    from PIL import Image

    with Image.open(BytesIO(data)) as image:
        return image.size


def test_valid_application_json():
    app = parse_application_text(VALID_JSON)
    assert app.product_type == "distilled_spirits"
    assert app.brand_name == "OLD TOM DISTILLERY"


def test_missing_required_field():
    with pytest.raises(InputValidationError, match="missing required"):
        parse_application_text('{"product_type":"wine","brand_name":"A","class_type":"B","alcohol_content":"1%"}')


def test_empty_required_field():
    with pytest.raises(InputValidationError, match="must not be empty"):
        parse_application_text(VALID_JSON.replace('"OLD TOM DISTILLERY"', '""'))


def test_invalid_product_type():
    with pytest.raises(InputValidationError, match="product_type"):
        parse_application_text(VALID_JSON.replace('"distilled_spirits"', '"cider"'))


def test_supported_image_extensions():
    assert is_supported_image_extension("label.jpg")
    assert is_supported_image_extension("label.jpeg")
    assert is_supported_image_extension("label.jpe")
    assert is_supported_image_extension("label.png")


def test_unsupported_image_extension_rejected():
    with pytest.raises(ImageValidationError, match="unsupported image extension"):
        prepare_image_collection([("label.gif", b"not-image", "image/gif")])


def test_corrupt_image_rejected():
    with pytest.raises(ImageValidationError, match="corrupt or unreadable"):
        prepare_image_collection([("label.png", b"not-image", "image/png")])


def test_image_count_limits():
    with pytest.raises(ImageValidationError, match="at least one"):
        prepare_image_collection([])
    files = [(f"label{i}.png", png_bytes(), "image/png") for i in range(11)]
    with pytest.raises(ImageValidationError, match="no more than 10"):
        prepare_image_collection(files)


def test_valid_image_collection():
    images = prepare_image_collection([("label.png", png_bytes(), "image/png")])
    assert len(images) == 1
    assert images[0].api_mime_type == "image/jpeg"


def test_smaller_label_images_are_upscaled_for_model_readability():
    from io import BytesIO

    from PIL import Image

    source = Image.new("RGB", (800, 297), (255, 255, 255))
    output = BytesIO()
    source.save(output, format="JPEG")

    prepared = prepare_image_collection(
        [("wide-label.jpg", output.getvalue(), "image/jpeg")],
        max_side_px=1600,
    )[0]

    assert (prepared.width, prepared.height) == (800, 297)
    assert image_size(prepared.api_bytes) == (1600, 594)
