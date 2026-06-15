from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import PurePosixPath
from typing import Dict, List, Optional

from src.config import AppConfig
from src.image_utils import ImageValidationError, PreparedImage, is_supported_image_extension, prepare_image
from src.results import cannot_verify_result
from src.schemas import ApplicationData, BatchPreviewRow, VerificationResult
from src.validation import InputValidationError, validate_application_object


@dataclass
class BatchReviewUnit:
    display_name: str
    json_filename: Optional[str] = None
    json_file_count: int = 0
    supported_image_count: int = 0
    application: Optional[ApplicationData] = None
    images: List[PreparedImage] = field(default_factory=list)
    unsupported_files: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.errors and self.application is not None and 1 <= len(self.images) <= 10

    @property
    def json_status(self) -> str:
        if self.json_file_count == 0:
            return "Missing"
        if self.json_file_count == 1:
            return "Found"
        return f"Found {self.json_file_count}"

    @property
    def validation_status(self) -> str:
        return "ready" if self.is_valid else "cannot_verify"

    @property
    def validation_details(self) -> str:
        details = list(self.errors)
        if self.unsupported_files:
            details.append("Unsupported file(s): " + ", ".join(self.unsupported_files))
        return "Ready" if not details else " ".join(details)


@dataclass
class BatchParseResult:
    units: List[BatchReviewUnit]
    archive_error: Optional[str] = None


def _is_unsafe_zip_path(filename: str) -> bool:
    normalized = filename.replace("\\", "/")
    path = PurePosixPath(normalized)
    if not path.parts:
        return True
    if normalized.startswith("/") or ":" in path.parts[0]:
        return True
    return any(part in {"..", ""} for part in path.parts)


def _is_ignored_zip_entry(filename: str) -> bool:
    parts = PurePosixPath(filename.replace("\\", "/")).parts
    return bool(parts) and (parts[0] == "__MACOSX" or parts[-1] == ".DS_Store")


def _wrapper_prefix_for_paths(filenames: List[str]) -> Optional[str]:
    paths = [PurePosixPath(name.replace("\\", "/")).parts for name in filenames]
    if not paths:
        return None

    top_level_dirs = {parts[0] for parts in paths if len(parts) >= 2}
    has_root_file = any(len(parts) == 1 for parts in paths)
    if has_root_file or len(top_level_dirs) != 1:
        return None

    wrapper = next(iter(top_level_dirs))
    has_direct_files_in_wrapper = any(len(parts) == 2 and parts[0] == wrapper for parts in paths)
    has_product_files_under_wrapper = any(len(parts) >= 3 and parts[0] == wrapper for parts in paths)
    if has_product_files_under_wrapper and not has_direct_files_in_wrapper:
        return wrapper
    return None


def _unit_name_for_parts(parts: tuple[str, ...], wrapper_prefix: Optional[str]) -> Optional[str]:
    if wrapper_prefix is None:
        return parts[0] if len(parts) >= 2 else None
    if len(parts) < 3 or parts[0] != wrapper_prefix:
        return None
    return parts[1]


def parse_batch_zip(filename: str, data: bytes, config: AppConfig) -> BatchParseResult:
    max_bytes = config.max_zip_mb * 1024 * 1024
    if not filename.lower().endswith(".zip"):
        return BatchParseResult([BatchReviewUnit(display_name="zip_archive", errors=["Upload a .zip file."])])
    if not data:
        return BatchParseResult([BatchReviewUnit(display_name="zip_archive", errors=["ZIP file is empty."])])
    if len(data) > max_bytes:
        return BatchParseResult(
            [BatchReviewUnit(display_name="zip_archive", errors=[f"ZIP file is larger than {config.max_zip_mb} MB."])]
        )

    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile:
        return BatchParseResult([BatchReviewUnit(display_name="zip_archive", errors=["ZIP file is corrupt or unreadable."])])

    with archive:
        file_infos: List[zipfile.ZipInfo] = []
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            if _is_ignored_zip_entry(name):
                continue
            if _is_unsafe_zip_path(name):
                return BatchParseResult(
                    [BatchReviewUnit(display_name="zip_archive", errors=[f"Unsafe ZIP path rejected: {info.filename}."])],
                    archive_error="Unsafe ZIP path rejected.",
                )
            file_infos.append(info)

        wrapper_prefix = _wrapper_prefix_for_paths([info.filename for info in file_infos])
        units: Dict[str, Dict[str, List[zipfile.ZipInfo]]] = {}
        root_errors: List[str] = []

        for info in file_infos:
            name = info.filename.replace("\\", "/")
            parts = PurePosixPath(name).parts
            unit_name = _unit_name_for_parts(parts, wrapper_prefix)
            if unit_name is None:
                root_errors.append(f"Root-level file is not allowed: {name}.")
                continue
            bucket = units.setdefault(unit_name, {"json": [], "images": [], "unsupported": [], "nested": []})
            expected_depth = 3 if wrapper_prefix else 2
            if len(parts) != expected_depth:
                bucket["nested"].append(info)
            elif name.lower().endswith(".json"):
                bucket["json"].append(info)
            elif is_supported_image_extension(name):
                bucket["images"].append(info)
            else:
                bucket["unsupported"].append(info)

        parsed_units: List[BatchReviewUnit] = []
        if root_errors:
            parsed_units.append(BatchReviewUnit(display_name="zip_root", errors=root_errors))

        if len(units) > config.max_batch_units:
            parsed_units.append(
                BatchReviewUnit(
                    display_name="zip_archive",
                    errors=[f"Batch contains {len(units)} review units; limit is {config.max_batch_units}."],
                )
            )
            return BatchParseResult(parsed_units, archive_error="Too many review units.")

        for unit_name in sorted(units):
            parsed_units.append(_parse_unit(archive, unit_name, units[unit_name], config))

    if not parsed_units:
        parsed_units.append(BatchReviewUnit(display_name="zip_archive", errors=["ZIP contains no review unit folders."]))
    return BatchParseResult(parsed_units)


def _parse_unit(
    archive: zipfile.ZipFile,
    unit_name: str,
    bucket: Dict[str, List[zipfile.ZipInfo]],
    config: AppConfig,
) -> BatchReviewUnit:
    unit = BatchReviewUnit(display_name=unit_name)
    json_files = bucket["json"]
    image_files = bucket["images"]
    unsupported_files = bucket["unsupported"]
    nested_files = bucket["nested"]

    unit.json_file_count = len(json_files)
    unit.supported_image_count = len(image_files)
    unit.unsupported_files = [info.filename for info in unsupported_files] + [
        f"{info.filename} (nested files are not allowed)" for info in nested_files
    ]

    if len(json_files) != 1:
        unit.errors.append(f"Expected exactly one JSON file, found {len(json_files)}.")
    else:
        unit.json_filename = PurePosixPath(json_files[0].filename).name
        try:
            data = json.loads(archive.read(json_files[0]).decode("utf-8"))
            unit.application = validate_application_object(data)
        except UnicodeDecodeError:
            unit.errors.append(f"{unit.json_filename}: JSON file must be UTF-8 encoded.")
        except json.JSONDecodeError as exc:
            unit.errors.append(f"{unit.json_filename}: invalid JSON at line {exc.lineno}, column {exc.colno}.")
        except InputValidationError as exc:
            unit.errors.append(f"{unit.json_filename}: {exc}")

    if not image_files:
        unit.errors.append("Expected at least one supported image file.")
    if len(image_files) > config.max_images_per_unit:
        unit.errors.append(f"Expected no more than {config.max_images_per_unit} supported image files.")

    for info in image_files[: config.max_images_per_unit]:
        try:
            unit.images.append(
                prepare_image(
                    PurePosixPath(info.filename).name,
                    archive.read(info),
                    content_type=None,
                    max_size_mb=config.max_image_mb,
                    max_side_px=config.image_max_side_px,
                    jpeg_quality=config.image_jpeg_quality,
                )
            )
        except ImageValidationError as exc:
            unit.errors.append(str(exc))

    if unit.unsupported_files:
        unit.errors.append("Folder contains unsupported file(s).")
    return unit


def preview_rows(units: List[BatchReviewUnit]) -> List[BatchPreviewRow]:
    return [
        BatchPreviewRow(
            review_unit=unit.display_name,
            json_status=unit.json_status,
            image_count=unit.supported_image_count,
            validation_status=unit.validation_status,  # type: ignore[arg-type]
            validation_details=unit.validation_details,
        )
        for unit in units
    ]


def invalid_unit_result(unit: BatchReviewUnit) -> VerificationResult:
    reason = unit.validation_details
    return cannot_verify_result(unit.display_name, reason, application=unit.application, fields=["review_unit"], summary=reason)
