from __future__ import annotations

import json
import shutil
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Sequence

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_DIR = ROOT / "sample_data"
WARNING = (
    "GOVERNMENT WARNING: (1) According to the Surgeon General, women should not drink alcoholic beverages "
    "during pregnancy because of the risk of birth defects. (2) Consumption of alcoholic beverages impairs "
    "your ability to drive a car or operate machinery, and may cause health problems."
)


def main() -> None:
    if SAMPLE_DIR.exists():
        shutil.rmtree(SAMPLE_DIR)
    _write_single_sample(
        "passing",
        {
            "product_type": "distilled_spirits",
            "brand_name": "OLD TOM DISTILLERY",
            "class_type": "Kentucky Straight Bourbon Whiskey",
            "alcohol_content": "45% Alc./Vol. (90 Proof)",
            "net_contents": "750 mL",
            "name_and_address": "Bottled by Old Tom Distillery, Frankfort, KY",
            "country_of_origin": None,
        },
        [
            [
                "OLD TOM DISTILLERY",
                "Kentucky Straight Bourbon Whiskey",
                "45% Alc./Vol. (90 Proof)",
                "750 mL",
            ],
            [
                "Bottled by Old Tom Distillery, Frankfort, KY",
                WARNING,
            ],
        ],
    )
    _write_single_sample(
        "mismatch",
        {
            "product_type": "wine",
            "brand_name": "CEDAR HILL WINE",
            "class_type": "Red Wine",
            "alcohol_content": "13.5% Alc./Vol.",
            "net_contents": "750 mL",
            "name_and_address": "Bottled by Cedar Hill Winery, Walla Walla, WA",
            "country_of_origin": None,
        },
        [
            [
                "CEDAR HILL WINE",
                "Red Wine",
                "14.2% Alc./Vol.",
                "750 mL",
            ],
            [
                "Bottled by Cedar Hill Winery, Walla Walla, WA",
                WARNING,
            ],
        ],
    )
    _write_single_sample(
        "missing-field",
        {
            "product_type": "malt_beverage",
            "brand_name": "RIVER MARKET LAGER",
            "class_type": "Lager",
            "alcohol_content": "5.0% Alc./Vol.",
            "name_and_address": "",
            "country_of_origin": None,
        },
        [
            [
                "RIVER MARKET LAGER",
                "Lager",
                "5.0% Alc./Vol.",
                "12 fl oz",
            ],
        ],
    )
    _write_batch_readme()
    _write_batch_zip()
    print(f"Sample data written to {SAMPLE_DIR}")


def _write_single_sample(name: str, application: dict, label_pages: Sequence[Sequence[str]]) -> None:
    folder = SAMPLE_DIR / "single" / name
    labels = folder / "labels"
    labels.mkdir(parents=True, exist_ok=True)
    (folder / "application.json").write_text(json.dumps(application, indent=2), encoding="utf-8")
    for index, lines in enumerate(label_pages, start=1):
        _write_label_image(labels / f"label{index}.png", lines)


def _write_label_image(path: Path, lines: Sequence[str]) -> None:
    is_warning_page = any("GOVERNMENT WARNING" in line for line in lines)
    image_size = (1800, 1200) if is_warning_page else (1400, 900)
    font = _font(44 if is_warning_page else 42)
    image = Image.new("RGB", image_size, (250, 249, 244))
    draw = ImageDraw.Draw(image)
    y = 80
    max_width = image_size[0] - 140
    for line in lines:
        for wrapped in _wrap_pixels(draw, line, font, max_width):
            draw.text((70, y), wrapped, fill=(22, 26, 31), font=font)
            y += 62 if is_warning_page else 58
        y += 26
    image.save(path)


def _font(size: int) -> ImageFont.ImageFont:
    for candidate in [
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("C:/Windows/Fonts/segoeui.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default()


def _wrap_pixels(draw: ImageDraw.ImageDraw, line: str, font: ImageFont.ImageFont, max_width: int) -> Sequence[str]:
    words = line.split()
    lines = []
    current = ""
    for word in words:
        next_line = f"{current} {word}".strip()
        bbox = draw.textbbox((0, 0), next_line, font=font)
        if bbox[2] - bbox[0] > max_width and current:
            lines.append(current)
            current = word
        else:
            current = next_line
    if current:
        lines.append(current)
    return lines or [""]


def _write_batch_readme() -> None:
    batch_dir = SAMPLE_DIR / "batch"
    batch_dir.mkdir(parents=True, exist_ok=True)
    (batch_dir / "README.md").write_text(
        "Run `python scripts/generate_sample_data.py` to recreate `sample_batch.zip`.\n",
        encoding="utf-8",
    )


def _write_batch_zip() -> None:
    batch_zip = SAMPLE_DIR / "batch" / "sample_batch.zip"
    with zipfile.ZipFile(batch_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for sample in ["passing", "mismatch", "missing-field"]:
            sample_folder = SAMPLE_DIR / "single" / sample
            unit_name = sample
            archive.write(sample_folder / "application.json", f"{unit_name}/application.json")
            for image in sorted((sample_folder / "labels").glob("*.png")):
                archive.write(image, f"{unit_name}/{image.name}")


if __name__ == "__main__":
    main()
