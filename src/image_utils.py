from __future__ import annotations

import base64
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
from typing import Iterable, List, Optional, Sequence, Tuple

from PIL import Image, ImageFilter, UnidentifiedImageError

from src.config import SUPPORTED_IMAGE_EXTENSIONS


class ImageValidationError(ValueError):
    pass


@dataclass(frozen=True)
class PreparedImage:
    filename: str
    original_content_type: Optional[str]
    original_size_bytes: int
    width: int
    height: int
    api_bytes: bytes
    api_mime_type: str = "image/jpeg"

    def data_url(self) -> str:
        encoded = base64.b64encode(self.api_bytes).decode("ascii")
        return f"data:{self.api_mime_type};base64,{encoded}"


def normalized_filename(filename: str) -> str:
    name = PurePosixPath(str(filename).replace("\\", "/")).name
    return name or "uploaded-image"


def file_extension(filename: str) -> str:
    return PurePosixPath(str(filename).lower().replace("\\", "/")).suffix


def is_supported_image_extension(filename: str) -> bool:
    return file_extension(filename) in SUPPORTED_IMAGE_EXTENSIONS


def validate_image_count(count: int, max_images: int = 10) -> None:
    if count < 1:
        raise ImageValidationError("Upload at least one label image.")
    if count > max_images:
        raise ImageValidationError(f"Upload no more than {max_images} label images per review unit.")


def _content_type_is_plausible(content_type: Optional[str]) -> bool:
    if not content_type:
        return True
    allowed = {"image/jpeg", "image/png", "image/jpg", "application/octet-stream"}
    return content_type.lower() in allowed


def prepare_image(
    filename: str,
    data: bytes,
    content_type: Optional[str] = None,
    max_size_mb: int = 10,
    max_side_px: int = 1600,
    jpeg_quality: int = 82,
) -> PreparedImage:
    safe_name = normalized_filename(filename)
    if not is_supported_image_extension(safe_name):
        raise ImageValidationError(
            f"{safe_name}: unsupported image extension. Use .jpg, .jpeg, .jpe, or .png."
        )
    if not data:
        raise ImageValidationError(f"{safe_name}: file is empty.")
    max_bytes = max_size_mb * 1024 * 1024
    if len(data) > max_bytes:
        raise ImageValidationError(f"{safe_name}: file is larger than the {max_size_mb} MB limit.")
    if not _content_type_is_plausible(content_type):
        raise ImageValidationError(f"{safe_name}: uploaded file does not look like a JPEG or PNG image.")

    try:
        with Image.open(BytesIO(data)) as image:
            image.load()
            if image.format not in {"JPEG", "PNG"}:
                raise ImageValidationError(f"{safe_name}: image content must be JPEG or PNG.")
            width, height = image.size
            if width <= 0 or height <= 0:
                raise ImageValidationError(f"{safe_name}: image dimensions are invalid.")
            image = _flatten_to_rgb(image)
            image = _resize_for_api_readability(image, max_side_px)
            output = BytesIO()
            image.save(output, format="JPEG", quality=jpeg_quality, optimize=True)
            return PreparedImage(
                filename=safe_name,
                original_content_type=content_type,
                original_size_bytes=len(data),
                width=width,
                height=height,
                api_bytes=output.getvalue(),
            )
    except ImageValidationError:
        raise
    except (UnidentifiedImageError, OSError) as exc:
        raise ImageValidationError(f"{safe_name}: corrupt or unreadable image file.") from exc


def _flatten_to_rgb(image: Image.Image) -> Image.Image:
    if image.mode in {"RGBA", "LA"}:
        background = Image.new("RGB", image.size, (255, 255, 255))
        alpha = image.getchannel("A")
        background.paste(image.convert("RGB"), mask=alpha)
        return background
    if image.mode != "RGB":
        return image.convert("RGB")
    return image.copy()


def _resize_for_api_readability(image: Image.Image, max_side_px: int) -> Image.Image:
    longest_side = max(image.size)
    if longest_side <= 0:
        return image.copy()
    if longest_side > max_side_px:
        resized = image.copy()
        resized.thumbnail((max_side_px, max_side_px), Image.Resampling.LANCZOS)
        return resized
    if longest_side >= 300 and longest_side < max_side_px:
        scale = max_side_px / longest_side
        resized = image.resize(
            (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
            Image.Resampling.LANCZOS,
        )
        return resized.filter(ImageFilter.UnsharpMask(radius=1.0, percent=130, threshold=3))
    return image.copy()


def prepare_image_collection(
    files: Sequence[Tuple[str, bytes, Optional[str]]],
    max_images: int = 10,
    max_size_mb: int = 10,
    max_side_px: int = 1600,
    jpeg_quality: int = 82,
) -> List[PreparedImage]:
    validate_image_count(len(files), max_images=max_images)
    return [
        prepare_image(
            filename,
            data,
            content_type,
            max_size_mb=max_size_mb,
            max_side_px=max_side_px,
            jpeg_quality=jpeg_quality,
        )
        for filename, data, content_type in files
    ]


def unsupported_filenames(filenames: Iterable[str]) -> List[str]:
    return [name for name in filenames if not is_supported_image_extension(name)]
