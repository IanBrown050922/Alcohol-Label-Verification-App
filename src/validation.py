from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from src.config import REQUIRED_APPLICATION_FIELDS, default_application_json
from src.schemas import ApplicationData


class InputValidationError(ValueError):
    pass


def parse_json_text(text: str) -> Any:
    if not text or not text.strip():
        raise InputValidationError("Application JSON is empty.")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise InputValidationError(f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}.") from exc


def validate_application_object(data: Any) -> ApplicationData:
    if not isinstance(data, dict):
        raise InputValidationError("Application JSON must be a JSON object.")
    missing = [field for field in REQUIRED_APPLICATION_FIELDS if field not in data]
    if missing:
        raise InputValidationError("Application JSON is missing required field(s): " + ", ".join(missing) + ".")
    try:
        return ApplicationData.model_validate(data)
    except ValidationError as exc:
        messages = []
        for error in exc.errors():
            loc = ".".join(str(part) for part in error.get("loc", []))
            msg = error.get("msg", "invalid value")
            messages.append(f"{loc}: {msg}" if loc else msg)
        raise InputValidationError("Application JSON validation failed: " + "; ".join(messages) + ".") from exc


def parse_application_text(text: str) -> ApplicationData:
    return validate_application_object(parse_json_text(text))


def format_application_json(data: Any) -> str:
    app = validate_application_object(data)
    return app.model_dump_json(indent=2)


def default_template_text() -> str:
    return default_application_json()
