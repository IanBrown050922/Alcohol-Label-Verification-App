from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sys
import time
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from PIL import Image, ImageFilter, UnidentifiedImageError


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_DIR = ROOT / "raw_data" / "cola-sample-pack-v1"
OUTPUT_DIR = ROOT / "sample_data" / "real_cola"
REVIEW_UNITS_DIR = OUTPUT_DIR / "review_units"
SINGLE_EXAMPLES_DIR = OUTPUT_DIR / "single_review_examples"
BATCH_ZIP_PATH = OUTPUT_DIR / "real_cola_batch.zip"
IMAGE_URL_TEMPLATE = "https://dyuie4zgfxmt6.cloudfront.net/{ttb_image_id}.webp"
MAX_IMAGES_PER_UNIT = 10
DEFAULT_TIMEOUT_SECONDS = 20
JPEG_QUALITY = 90
REQUIRED_GOVERNMENT_WARNING_TEXT = (
    "GOVERNMENT WARNING: (1) According to the Surgeon General, women should not "
    "drink alcoholic beverages during pregnancy because of the risk of birth "
    "defects. (2) Consumption of alcoholic beverages impairs your ability to "
    "drive a car or operate machinery, and may cause health problems."
)

US_STATES = {
    "alabama",
    "alaska",
    "arizona",
    "arkansas",
    "california",
    "colorado",
    "connecticut",
    "delaware",
    "district of columbia",
    "florida",
    "georgia",
    "hawaii",
    "idaho",
    "illinois",
    "indiana",
    "iowa",
    "kansas",
    "kentucky",
    "louisiana",
    "maine",
    "maryland",
    "massachusetts",
    "michigan",
    "minnesota",
    "mississippi",
    "missouri",
    "montana",
    "nebraska",
    "nevada",
    "new hampshire",
    "new jersey",
    "new mexico",
    "new york",
    "north carolina",
    "north dakota",
    "ohio",
    "oklahoma",
    "oregon",
    "pennsylvania",
    "rhode island",
    "south carolina",
    "south dakota",
    "tennessee",
    "texas",
    "utah",
    "vermont",
    "virginia",
    "washington",
    "west virginia",
    "wisconsin",
    "wyoming",
    "ak",
    "al",
    "ar",
    "az",
    "ca",
    "co",
    "ct",
    "dc",
    "de",
    "fl",
    "ga",
    "hi",
    "ia",
    "id",
    "il",
    "in",
    "ks",
    "ky",
    "la",
    "ma",
    "md",
    "me",
    "mi",
    "mn",
    "mo",
    "ms",
    "mt",
    "nc",
    "nd",
    "ne",
    "nh",
    "nj",
    "nm",
    "nv",
    "ny",
    "oh",
    "ok",
    "or",
    "pa",
    "ri",
    "sc",
    "sd",
    "tn",
    "tx",
    "ut",
    "va",
    "vt",
    "wa",
    "wi",
    "wv",
    "wy",
}

COUNTRY_NAMES = {
    "argentina",
    "australia",
    "austria",
    "belgium",
    "brazil",
    "canada",
    "chile",
    "china",
    "france",
    "germany",
    "greece",
    "ireland",
    "italy",
    "japan",
    "mexico",
    "netherlands",
    "new zealand",
    "portugal",
    "south africa",
    "spain",
    "united kingdom",
}


class DataPreparationError(RuntimeError):
    pass


def main() -> None:
    args = parse_args()
    input_dir = args.input.resolve()

    print(f"Reading COLA CSV files from {display_path(input_dir)}")
    cola_rows = read_csv(input_dir / "cola.csv")
    image_rows = read_csv(input_dir / "cola_image.csv")
    images_by_ttb_id = group_images_by_ttb_id(image_rows)
    print(f"Read {len(cola_rows)} COLA records and {len(image_rows)} image rows")

    reset_output_dir()
    REVIEW_UNITS_DIR.mkdir(parents=True, exist_ok=True)
    SINGLE_EXAMPLES_DIR.mkdir(parents=True, exist_ok=True)

    manifest_units: list[dict[str, Any]] = []
    skipped = Counter()
    failed_downloads: list[dict[str, str]] = []
    passing_candidates: list[dict[str, Any]] = []
    other_candidates: list[dict[str, Any]] = []

    needed_base_units = args.passing + args.cannot_verify
    print("Scanning real source units")

    for row in cola_rows:
        selected = build_real_unit_candidate(row, images_by_ttb_id, skipped)
        if selected is None:
            continue
        if is_passing_source_candidate(row, selected["image_rows"], selected["application"]):
            passing_candidates.append(selected)
        else:
            other_candidates.append(selected)

    print(f"Found {len(passing_candidates)} pass-oriented candidates and {len(other_candidates)} other usable candidates")
    print(f"Downloading images for {args.passing} passing source unit(s)")
    passing_units = materialize_candidates(
        passing_candidates,
        args.passing,
        args.timeout,
        failed_downloads,
        skipped,
        label="passing",
    )
    if len(passing_units) < args.passing:
        print("  warning: filling remaining passing units from other usable candidates")
        passing_units.extend(
            materialize_candidates(
                [candidate for candidate in other_candidates if candidate["ttb_id"] not in {unit["ttb_id"] for unit in passing_units}],
                args.passing - len(passing_units),
                args.timeout,
                failed_downloads,
                skipped,
                label="passing-fallback",
            )
        )

    used_ttb_ids = {unit["ttb_id"] for unit in passing_units}
    print(f"Downloading images for {args.cannot_verify} cannot-verify source unit(s)")
    cannot_verify_sources = materialize_candidates(
        [candidate for candidate in other_candidates + passing_candidates if candidate["ttb_id"] not in used_ttb_ids],
        args.cannot_verify,
        args.timeout,
        failed_downloads,
        skipped,
        label="cannot-verify",
    )

    if len(passing_units) < args.passing or len(cannot_verify_sources) < args.cannot_verify:
        raise DataPreparationError(
            f"Only {len(passing_units)} passing and {len(cannot_verify_sources)} cannot-verify source units were found; "
            f"need {needed_base_units} total. "
            "Check source data and image download failures."
        )

    mismatch_sources = [passing_units[index % len(passing_units)] for index in range(args.mismatches)] if passing_units else []

    print("Writing passing review units")
    written_passing = []
    for index, source in enumerate(passing_units, start=1):
        folder_name = f"passing_{index:03d}_{source['ttb_id']}"
        unit = write_unit(folder_name, "passing", source, source["application"], mutation=None)
        manifest_units.append(unit)
        written_passing.append(unit)

    print("Writing mismatch review units")
    written_mismatch = []
    for index, source in enumerate(mismatch_sources, start=1):
        application, mutation = mutate_application(source["application"], index)
        folder_name = f"mismatch_{index:03d}_{source['ttb_id']}"
        unit = write_unit(folder_name, "mismatch", source, application, mutation=mutation)
        manifest_units.append(unit)
        written_mismatch.append(unit)

    print("Writing cannot-verify review units")
    written_cannot_verify = []
    cannot_verify_rules = [
        ("blurred_downscaled_image", write_blurred_downscaled_images),
        ("missing_required_field_net_contents", write_normal_images),
        ("corrupt_label_image", write_corrupt_image),
    ]
    for index, source in enumerate(cannot_verify_sources, start=1):
        reason, image_writer = cannot_verify_rules[(index - 1) % len(cannot_verify_rules)]
        application = dict(source["application"])
        if reason == "missing_required_field_net_contents":
            application.pop("net_contents", None)
        folder_name = f"cannot_verify_{index:03d}_{reason}_{source['ttb_id']}"
        unit = write_unit(
            folder_name,
            "cannot_verify",
            source,
            application,
            mutation=None,
            image_writer=image_writer,
            cannot_verify_reason=reason,
        )
        manifest_units.append(unit)
        written_cannot_verify.append(unit)

    write_single_examples(written_passing, written_mismatch, written_cannot_verify)
    write_batch_zip()

    manifest = {
        "source": "COLA Cloud sample pack",
        "source_input_dir": display_path(input_dir),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "records_read": len(cola_rows),
        "image_rows_read": len(image_rows),
        "passing_units_created": len(written_passing),
        "mismatch_units_created": len(written_mismatch),
        "cannot_verify_units_created": len(written_cannot_verify),
        "skipped_records": dict(sorted(skipped.items())),
        "failed_image_downloads": failed_downloads,
        "units": manifest_units,
    }
    write_json(OUTPUT_DIR / "manifest.json", manifest)
    write_readme(args, manifest)

    print("")
    print("Generated real COLA sample data")
    print(f"  passing units:        {len(written_passing)}")
    print(f"  mismatch units:       {len(written_mismatch)}")
    print(f"  cannot-verify units:  {len(written_cannot_verify)}")
    print(f"  review units:         {display_path(REVIEW_UNITS_DIR)}")
    print(f"  single examples:      {display_path(SINGLE_EXAMPLES_DIR)}")
    print(f"  batch ZIP:            {display_path(BATCH_ZIP_PATH)}")
    if skipped:
        print(f"  skipped records:      {dict(sorted(skipped.items()))}")
    if failed_downloads:
        print(f"  failed downloads:     {len(failed_downloads)}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare app-ready review units from the COLA Cloud sample pack.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT_DIR, help="Input COLA CSV directory.")
    parser.add_argument("--passing", type=int, default=20, help="Number of passing real review units to create.")
    parser.add_argument("--mismatches", type=int, default=5, help="Number of generated mismatch units to create.")
    parser.add_argument("--cannot-verify", type=int, default=3, help="Number of cannot-verify units to create.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Image download timeout in seconds.")
    args = parser.parse_args()
    for name in ("passing", "mismatches", "cannot_verify"):
        if getattr(args, name) < 0:
            parser.error(f"--{name.replace('_', '-')} must be non-negative")
    if args.mismatches and args.passing == 0:
        parser.error("--mismatches requires at least one --passing unit to copy")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    return args


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise DataPreparationError(f"Missing required input file: {display_path(path)}")
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def group_images_by_ttb_id(image_rows: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in image_rows:
        ttb_id = clean(row.get("TTB_ID"))
        if ttb_id:
            grouped[ttb_id].append(row)
    for rows in grouped.values():
        rows.sort(key=image_sort_key)
    return grouped


def image_sort_key(row: dict[str, str]) -> tuple[int, int, str]:
    position = clean(row.get("CONTAINER_POSITION")).lower()
    position_rank = {"front": 0, "main": 0, "back": 1, "neck": 2, "strip": 3, "other": 4}.get(position, 5)
    return (position_rank, parse_int(row.get("IMAGE_INDEX"), 999), clean(row.get("TTB_IMAGE_ID")))


def build_real_unit_candidate(
    row: dict[str, str],
    images_by_ttb_id: dict[str, list[dict[str, str]]],
    skipped: Counter,
) -> dict[str, Any] | None:
    ttb_id = clean(row.get("TTB_ID"))
    if not ttb_id:
        skipped["missing_ttb_id"] += 1
        return None

    product_type = normalize_product_type(row.get("PRODUCT_TYPE"))
    if not product_type:
        skipped["unsupported_product_type"] += 1
        return None

    brand_name = clean(row.get("BRAND_NAME"))
    class_type = clean(row.get("CLASS_NAME"))
    alcohol_content = clean(row.get("OCR_ABV"))
    volume = clean(row.get("OCR_VOLUME"))
    volume_unit = clean(row.get("OCR_VOLUME_UNIT"))
    if not all([brand_name, class_type, alcohol_content, volume, volume_unit]):
        skipped["missing_required_metadata"] += 1
        return None

    image_rows = [
        image_row
        for image_row in images_by_ttb_id.get(ttb_id, [])
        if clean(image_row.get("TTB_IMAGE_ID")) and clean(image_row.get("IS_OPENABLE")).lower() != "false"
    ][:MAX_IMAGES_PER_UNIT]
    if not image_rows:
        skipped["no_related_openable_images"] += 1
        return None

    application = {
        "product_type": product_type,
        "brand_name": brand_name,
        "class_type": class_type,
        "alcohol_content": alcohol_content,
        "net_contents": f"{volume} {volume_unit}",
        "name_and_address": name_and_address(row),
        "country_of_origin": country_of_origin(row),
    }
    return {
        "ttb_id": ttb_id,
        "application": application,
        "image_rows": image_rows,
    }


def normalize_product_type(value: str | None) -> str | None:
    normalized = clean(value).lower().replace("_", " ").replace("-", " ")
    normalized = " ".join(normalized.split())
    if normalized == "wine":
        return "wine"
    if normalized in {"malt beverage", "beer"} or "malt beverage" in normalized:
        return "malt_beverage"
    if "distilled" in normalized and "spirit" in normalized:
        return "distilled_spirits"
    return None


def name_and_address(row: dict[str, str]) -> str | None:
    return clean(row.get("ORIGIN_NAME")) or clean(row.get("ADDRESS_STATE")) or None


def country_of_origin(row: dict[str, str]) -> str | None:
    if clean(row.get("DOMESTIC_OR_IMPORTED")).lower() != "imported":
        return None
    for field in ("ORIGIN_NAME", "ADDRESS_STATE"):
        value = clean(row.get(field)).lower()
        if value in COUNTRY_NAMES and value not in US_STATES:
            return value.title()
    return None


def is_passing_source_candidate(
    cola_row: dict[str, str],
    image_rows: list[dict[str, str]],
    application: dict[str, Any],
) -> bool:
    qualification_text = normalized_match_text(clean(cola_row.get("APPROVAL_QUALIFICATIONS")))
    if "government warning" in qualification_text or "gws" in qualification_text:
        return False

    ocr_text = normalized_match_text(" ".join(clean(row.get("OCR_TEXT")) for row in image_rows))
    if normalized_match_text(REQUIRED_GOVERNMENT_WARNING_TEXT) not in ocr_text:
        return False
    if normalized_match_text(application["brand_name"]) not in ocr_text:
        return False
    if not abv_value_matches_ocr(application["alcohol_content"], " ".join(clean(row.get("OCR_TEXT")) for row in image_rows)):
        return False

    volume, _, unit = application["net_contents"].partition(" ")
    if normalized_numeric(volume) not in ocr_text:
        return False
    return any(unit_variant in ocr_text for unit_variant in volume_unit_match_variants(unit))


def normalized_match_text(value: str) -> str:
    return " ".join("".join(char.lower() if char.isalnum() else " " for char in value).split())


def normalized_numeric(value: str) -> str:
    try:
        return f"{float(clean(value).replace('%', '').split()[0]):g}"
    except (ValueError, IndexError):
        return normalized_match_text(value)


def abv_value_matches_ocr(value: str, ocr_text: str) -> bool:
    try:
        number = float(clean(value).replace("%", "").split()[0])
    except (ValueError, IndexError):
        return normalized_match_text(value) in normalized_match_text(ocr_text)
    number_patterns = {re.escape(f"{number:g}"), re.escape(f"{number:.1f}")}
    joined = "|".join(number_patterns)
    patterns = [
        rf"\b(?:{joined})\s*%\s*(?:alc|abv|vol|by\s*vol)",
        rf"\balc(?:ohol)?\.?\s*(?:by\s*vol(?:ume)?\.?)?\s*(?:{joined})\b",
        rf"\b(?:{joined})\s*(?:alc|alcohol)\b",
    ]
    return any(re.search(pattern, ocr_text, flags=re.IGNORECASE) for pattern in patterns)


def volume_unit_match_variants(unit: str) -> set[str]:
    normalized = normalized_match_text(unit)
    variants = {normalized}
    if normalized in {"milliliters", "milliliter"}:
        variants.update({"ml", "m l"})
    elif normalized in {"liters", "liter"}:
        variants.add("l")
    elif normalized in {"pints", "pint"}:
        variants.update({"pint", "pints", "pt"})
    elif normalized in {"fluid ounces", "fluid ounce"}:
        variants.update({"fl oz", "fluid ounces", "fluid ounce"})
    elif normalized in {"gallons", "gallon"}:
        variants.update({"gal", "gallons", "gallon"})
    return variants


def materialize_candidates(
    candidates: list[dict[str, Any]],
    needed: int,
    timeout: int,
    failed_downloads: list[dict[str, str]],
    skipped: Counter,
    label: str,
) -> list[dict[str, Any]]:
    materialized: list[dict[str, Any]] = []
    for candidate in candidates:
        if len(materialized) >= needed:
            break
        image_results = download_images(
            candidate["ttb_id"],
            candidate["image_rows"],
            timeout,
            failed_downloads,
        )
        if not image_results:
            skipped["all_image_downloads_failed"] += 1
            continue
        unit = dict(candidate)
        unit["image_results"] = image_results
        materialized.append(unit)
        print(f"  selected {label} {len(materialized):02d}/{needed}: {unit['ttb_id']} ({len(image_results)} image(s))")
    return materialized


def download_images(
    ttb_id: str,
    image_rows: list[dict[str, str]],
    timeout: int,
    failed_downloads: list[dict[str, str]],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for row in image_rows[:MAX_IMAGES_PER_UNIT]:
        image_id = clean(row.get("TTB_IMAGE_ID"))
        if not image_id:
            continue
        try:
            jpeg_bytes = download_and_convert_image(image_id, timeout)
        except Exception as exc:
            failed_downloads.append({"ttb_id": ttb_id, "ttb_image_id": image_id, "error": str(exc)})
            print(f"  warning: {ttb_id}/{image_id} skipped: {exc}")
            continue
        results.append({"ttb_image_id": image_id, "jpeg_bytes": jpeg_bytes})
    return results


def download_and_convert_image(ttb_image_id: str, timeout: int) -> bytes:
    url = IMAGE_URL_TEMPLATE.format(ttb_image_id=ttb_image_id)
    request = Request(url, headers={"User-Agent": "alcohol-label-verification-sample-data/1.0"})
    last_error: Exception | None = None
    for attempt in range(1, 3):
        try:
            with urlopen(request, timeout=timeout) as response:
                status = getattr(response, "status", 200)
                if status >= 400:
                    raise DataPreparationError(f"HTTP {status}")
                data = response.read()
            return convert_webp_bytes_to_jpeg(data)
        except (HTTPError, URLError, TimeoutError, OSError, UnidentifiedImageError, DataPreparationError) as exc:
            last_error = exc
            if attempt == 1:
                time.sleep(0.5)
    raise DataPreparationError(str(last_error) if last_error else "download failed")


def convert_webp_bytes_to_jpeg(data: bytes) -> bytes:
    if not data:
        raise DataPreparationError("empty image response")
    with Image.open(BytesIO(data)) as image:
        image.load()
        if image.width <= 0 or image.height <= 0:
            raise DataPreparationError("invalid image dimensions")
        image = flatten_to_rgb(image)
        output = BytesIO()
        image.save(output, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return output.getvalue()


def flatten_to_rgb(image: Image.Image) -> Image.Image:
    if image.mode in {"RGBA", "LA"}:
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image.convert("RGB"), mask=image.getchannel("A"))
        return background
    if image.mode != "RGB":
        return image.convert("RGB")
    return image.copy()


def write_unit(
    folder_name: str,
    kind: str,
    source: dict[str, Any],
    application: dict[str, Any],
    mutation: dict[str, Any] | None,
    image_writer: Any | None = None,
    cannot_verify_reason: str | None = None,
) -> dict[str, Any]:
    safe_name = safe_folder_name(folder_name)
    folder = REVIEW_UNITS_DIR / safe_name
    folder.mkdir(parents=True, exist_ok=False)
    write_json(folder / "application.json", application)

    writer = image_writer or write_normal_images
    writer(folder, source["image_results"])

    manifest_unit = {
        "folder": display_path(folder),
        "kind": kind,
        "source_ttb_id": source["ttb_id"],
        "source_image_ids": [image["ttb_image_id"] for image in source["image_results"]],
        "application_json": application,
        "mutation": mutation,
    }
    if cannot_verify_reason:
        manifest_unit["cannot_verify_reason"] = cannot_verify_reason
    return manifest_unit


def write_normal_images(folder: Path, image_results: list[dict[str, Any]]) -> None:
    for index, image in enumerate(image_results[:MAX_IMAGES_PER_UNIT], start=1):
        (folder / f"label_{index}.jpg").write_bytes(image["jpeg_bytes"])


def write_blurred_downscaled_images(folder: Path, image_results: list[dict[str, Any]]) -> None:
    source = image_results[0]["jpeg_bytes"]
    with Image.open(BytesIO(source)) as image:
        image.load()
        image = flatten_to_rgb(image)
        image.thumbnail((80, 80))
        image = image.resize((320, 320), Image.Resampling.BILINEAR)
        image = image.filter(ImageFilter.GaussianBlur(radius=6))
        output = BytesIO()
        image.save(output, format="JPEG", quality=55)
    (folder / "label_1.jpg").write_bytes(output.getvalue())


def write_corrupt_image(folder: Path, image_results: list[dict[str, Any]]) -> None:
    _ = image_results
    (folder / "label_1.jpg").write_bytes(b"not a valid jpeg image\n")


def mutate_application(application: dict[str, Any], index: int) -> tuple[dict[str, Any], dict[str, Any]]:
    mutated = dict(application)
    strategies = [
        mutate_alcohol_content,
        mutate_net_contents,
        mutate_brand_name,
    ]
    strategy = strategies[(index - 1) % len(strategies)]
    field, original, new_value = strategy(mutated)
    mutated[field] = new_value
    return mutated, {"field": field, "original_value": original, "new_value": new_value}


def mutate_alcohol_content(application: dict[str, Any]) -> tuple[str, str, str]:
    original = str(application["alcohol_content"])
    try:
        number = float(original.replace("%", "").split()[0])
        replacement = f"{number + 1.0:g}"
        if replacement == original:
            replacement = f"{number + 2.0:g}"
    except ValueError:
        replacement = "45% Alc./Vol." if "45" not in original else "40% Alc./Vol."
    return "alcohol_content", original, replacement


def mutate_net_contents(application: dict[str, Any]) -> tuple[str, str, str]:
    original = str(application["net_contents"])
    replacement = "1 liter"
    if original.strip().lower() == replacement:
        replacement = "750 milliliters"
    return "net_contents", original, replacement


def mutate_brand_name(application: dict[str, Any]) -> tuple[str, str, str]:
    original = str(application["brand_name"])
    replacement = "Heritage Reserve"
    if original.strip().lower() == replacement.lower():
        replacement = "North Coast Cellars"
    return "brand_name", original, replacement


def write_single_examples(
    passing_units: list[dict[str, Any]],
    mismatch_units: list[dict[str, Any]],
    cannot_verify_units: list[dict[str, Any]],
) -> None:
    examples = {
        "passing": passing_units[0] if passing_units else None,
        "mismatch": mismatch_units[0] if mismatch_units else None,
        "cannot_verify": cannot_verify_units[0] if cannot_verify_units else None,
    }
    for name, unit in examples.items():
        if unit is None:
            continue
        source_folder = ROOT / unit["folder"]
        target_folder = SINGLE_EXAMPLES_DIR / name
        target_folder.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_folder / "application.json", target_folder / "application.json")
        for image in sorted(source_folder.glob("*.jpg")):
            shutil.copy2(image, target_folder / image.name)


def write_batch_zip() -> None:
    with zipfile.ZipFile(BATCH_ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for unit_folder in sorted(REVIEW_UNITS_DIR.iterdir()):
            if not unit_folder.is_dir():
                continue
            for path in sorted(unit_folder.iterdir()):
                if not path.is_file():
                    continue
                relative = PurePosixPath(unit_folder.name) / path.name
                if is_unsafe_zip_path(relative):
                    raise DataPreparationError(f"Unsafe generated ZIP path: {relative}")
                archive.write(path, relative.as_posix())


def is_unsafe_zip_path(path: PurePosixPath) -> bool:
    parts = path.parts
    return not parts or any(part in {"", ".", ".."} for part in parts) or path.is_absolute()


def write_readme(args: argparse.Namespace, manifest: dict[str, Any]) -> None:
    readme = f"""# Real COLA Sample Data

This directory contains app-ready sample review units generated from the COLA Cloud sample pack.
The raw extracted CSV package lives under `{display_path(args.input.resolve())}` and is based on public TTB COLA records.

The generator reads `cola.csv` and `cola_image.csv`, links records with `cola.TTB_ID -> cola_image.TTB_ID`, downloads label images from:

```text
https://dyuie4zgfxmt6.cloudfront.net/{{TTB_IMAGE_ID}}.webp
```

Source images are WebP. The app supports `.jpg`, `.jpeg`, `.jpe`, and `.png`, so the generator validates each downloaded image with Pillow and saves it as JPEG.

## What Was Generated

- Passing real records: {manifest["passing_units_created"]}
- Mismatch records: {manifest["mismatch_units_created"]}
- Cannot-verify records: {manifest["cannot_verify_units_created"]}
- Batch ZIP: `real_cola_batch.zip`
- Unzipped review units: `review_units/`
- Single Review examples: `single_review_examples/`

Passing records use public real COLA metadata and public real label images. Mismatch examples keep real label images but modify exactly one field in `application.json`. Cannot-verify examples are intentionally damaged or incomplete for testing, such as a blurred/downscaled label image, a missing required JSON field, or a corrupt `.jpg` payload.

## Single Review

Use `single_review_examples/passing/`, `single_review_examples/mismatch/`, or `single_review_examples/cannot_verify/`.
Upload that folder's `application.json` and the `label_*.jpg` images in the app's Single Review mode.

## Batch Review

Upload `real_cola_batch.zip` in Batch Review mode. The ZIP contains one top-level folder per review unit. Each folder contains one `application.json` file and one to ten `label_*.jpg` images.

## Regenerate

```bash
python scripts/prepare_real_cola_data.py --input raw_data/cola-sample-pack-v1 --passing 20 --mismatches 5 --cannot-verify 3
```

Generated image folders and ZIP files are intentionally ignored by Git because they can be large. Regenerate them locally with the script when needed. `manifest.json`, this README, and the generator script are intended to be commit-friendly.
"""
    (OUTPUT_DIR / "README.md").write_text(readme, encoding="utf-8")


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def reset_output_dir() -> None:
    resolved_output = OUTPUT_DIR.resolve()
    resolved_sample_data = (ROOT / "sample_data").resolve()
    if resolved_sample_data not in resolved_output.parents:
        raise DataPreparationError(f"Refusing to delete unexpected output path: {resolved_output}")
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def safe_folder_name(value: str) -> str:
    safe_chars = []
    for char in value:
        if char.isalnum() or char in {"-", "_"}:
            safe_chars.append(char)
        else:
            safe_chars.append("_")
    cleaned = "".join(safe_chars).strip("._")
    if not cleaned:
        raise DataPreparationError("Generated empty folder name")
    return cleaned[:140]


def parse_int(value: str | None, default: int) -> int:
    try:
        return int(float(clean(value)))
    except ValueError:
        return default


def clean(value: str | None) -> str:
    if value is None:
        return ""
    stripped = str(value).strip()
    if stripped.lower() in {"", "none", "null", "nan"}:
        return ""
    return stripped


def display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


if __name__ == "__main__":
    try:
        main()
    except DataPreparationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
