from __future__ import annotations

import hashlib
import html
from typing import List, Sequence

import streamlit as st

from src.batch import BatchReviewUnit, invalid_unit_result, parse_batch_zip, preview_rows
from src.config import NO_STORAGE_NOTICE, AppConfig, default_application_json, load_config
from src.image_utils import ImageValidationError, PreparedImage, prepare_image_collection
from src.openai_verifier import verify_review_unit
from src.results import results_to_json, single_result_to_json, summary_counts
from src.schemas import VerificationResult
from src.validation import InputValidationError, format_application_json, parse_application_text, parse_json_text


MAX_JSON_UPLOAD_MB = 2
PREVIEW_ROW_LIMIT = 10


def main() -> None:
    st.set_page_config(page_title="Alcohol Label Verification", layout="wide")
    _inject_css()
    config = load_config(use_streamlit_secrets=True)
    _init_session_state()

    _render_header()

    active_mode = _render_mode_selector()
    if active_mode == "single":
        _render_single_review(config)
    else:
        _render_batch_review(config)


def _init_session_state() -> None:
    if "single_json_editor" not in st.session_state:
        st.session_state["single_json_editor"] = default_application_json()


def _render_mode_selector() -> str:
    selected = st.radio(
        "",
        ["Single Review", "Batch Review"],
        index=0,
        key="review_mode_label",
        horizontal=True,
    )
    active_mode = "batch" if selected == "Batch Review" else "single"
    st.markdown(f'<div class="mode-rule mode-rule-{_html(active_mode)}"></div>', unsafe_allow_html=True)
    return active_mode


def _render_header() -> None:
    st.markdown(
        """
        <section class="hero">
          <h1>Alcohol Label Verifier</h1>
          <p>
            Welcome to the Alcohol Label Verifier!
          </p>
          <p>
            Compare a product's application details against the product's label information. Use <strong>Single Review</strong> for one product, or <strong>Batch Review</strong>
            when you have a ZIP file with multiple products to check.
          </p>
        </section>
        """,
        unsafe_allow_html=True,
    )


def _render_single_review(config: AppConfig) -> None:
    st.markdown('<div class="mode-intro">Review one product\'s application against its label images.</div>', unsafe_allow_html=True)

    st.markdown(
        """
        <div class="section-heading">
          <div>
            <h2>1. Application Details</h2>
            <p>Edit the example values below to match the product you are reviewing, or load in a JSON file with the product information.</p>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown('<div class="json-entry-panel">', unsafe_allow_html=True)
    action_left, action_right = st.columns([1, 3.5])
    with action_left:
        st.markdown('<div class="reset-button-spacer"></div>', unsafe_allow_html=True)
        if st.button("↻\u00a0\u00a0Reset example values", key="reset_example_json"):
            st.session_state["single_json_editor"] = default_application_json()
            st.session_state.pop("single_result", None)
            st.session_state.pop("last_json_upload_sig", None)
            st.session_state["suppress_next_json_autoload"] = True
            _rerun()
    with action_right:
        uploaded_json = st.file_uploader(
            "Load a JSON file into the editor",
            type=["json"],
            accept_multiple_files=False,
            key="single_json_file",
            help=f"JSON only. Keep files under {MAX_JSON_UPLOAD_MB} MB.",
            on_change=_handle_single_json_upload,
        )

    _maybe_load_uploaded_json(uploaded_json)

    if st.session_state.get("single_json_message"):
        st.success(st.session_state.pop("single_json_message"))
    if st.session_state.get("single_json_error"):
        st.error(st.session_state.pop("single_json_error"))

    st.markdown(
        '<p class="field-note">Replace the example values with the product information from the application. Keep the field names and quotation marks intact.</p>',
        unsafe_allow_html=True,
    )
    st.text_area("", key="single_json_editor", height=330)
    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown(
        """
        <div class="section-heading image-section">
          <div>
            <h2>2. Label Images</h2>
            <p>Upload every label image for this product. Images can be front, back, side, neck, or any other label type.</p>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.caption(f"Upload 1-10 images (JPEG or PNG) up to {config.max_image_mb} MB each. {NO_STORAGE_NOTICE}")
    uploaded_images = st.file_uploader(
        "Upload label images",
        type=["jpg", "jpeg", "jpe", "png"],
        accept_multiple_files=True,
        key="single_images",
        help="All uploaded images are treated as one evidence set.",
    )
    if uploaded_images:
        image_names = ", ".join(getattr(uploaded, "name", "image") for uploaded in uploaded_images)
        st.caption(f"{len(uploaded_images)} image file(s) selected: {image_names}")

    st.markdown(
        """
        <div class="section-heading verify-section">
          <div>
            <h2>3. Run Verification</h2>
            <p>Verify the application details and label images once all data is present.</p>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if _centered_verify_button("Verify label", "single_verify_button"):
        _run_single_review(config, uploaded_images or [])
    if st.session_state.get("single_verify_notice"):
        st.error(st.session_state.pop("single_verify_notice"))

    result = st.session_state.get("single_result")
    if result is not None:
        _render_result(result, download_key="single_download")


def _handle_single_json_upload() -> None:
    _maybe_load_uploaded_json(st.session_state.get("single_json_file"))


def _maybe_load_uploaded_json(uploaded_json: object) -> None:
    if uploaded_json is None:
        return
    if isinstance(uploaded_json, list):
        if len(uploaded_json) == 0:
            return
        if len(uploaded_json) > 1:
            st.session_state["single_json_error"] = (
                "Upload one application JSON file only. Current editor content was preserved."
            )
            return
        uploaded_json = uploaded_json[0]

    data = uploaded_json.getvalue()
    signature = f"{getattr(uploaded_json, 'name', '')}:{hashlib.sha256(data).hexdigest()}"
    if st.session_state.pop("suppress_next_json_autoload", False):
        st.session_state["last_json_upload_sig"] = signature
        return
    if st.session_state.get("last_json_upload_sig") == signature:
        return
    st.session_state["last_json_upload_sig"] = signature

    max_bytes = MAX_JSON_UPLOAD_MB * 1024 * 1024
    if len(data) > max_bytes:
        st.session_state["single_json_error"] = (
            f"The JSON file is larger than {MAX_JSON_UPLOAD_MB} MB. Current editor content was preserved."
        )
        return

    try:
        parsed = parse_json_text(data.decode("utf-8"))
        st.session_state["single_json_editor"] = format_application_json(parsed)
        st.session_state["single_json_message"] = "JSON file loaded into the editor."
    except UnicodeDecodeError:
        st.session_state["single_json_error"] = "JSON file must be UTF-8 encoded. Current editor content was preserved."
    except InputValidationError as exc:
        st.session_state["single_json_error"] = f"{exc} Current editor content was preserved."


def _run_single_review(config: AppConfig, uploaded_images: Sequence[object]) -> None:
    st.session_state.pop("single_verify_notice", None)
    try:
        application = parse_application_text(st.session_state["single_json_editor"])
    except InputValidationError as exc:
        st.error(str(exc))
        return

    try:
        images = _prepare_streamlit_images(uploaded_images, config)
    except ImageValidationError as exc:
        st.error(str(exc))
        return

    with st.spinner("Checking the label images..."):
        result = verify_review_unit(application, images, "single-review", config)
    if result.overall_status == "cannot_verify":
        if not config.can_call_openai:
            st.session_state["single_verify_notice"] = (
                "The label could not be checked because the OpenAI API key is not configured. "
                "Add OPENAI_API_KEY to your local environment or Streamlit Cloud secrets."
            )
        elif result.summary.startswith("OpenAI/model verification failed") or result.summary.startswith(
            "Model response did not match"
        ):
            st.session_state["single_verify_notice"] = (
                "The label could not be checked because the model connection or response format failed. "
                "Confirm the API key, model name, and account access, then try again."
            )
    st.session_state["single_result"] = result


def _render_batch_review(config: AppConfig) -> None:
    st.markdown('<div class="mode-intro">Review multiple products from one ZIP file.</div>', unsafe_allow_html=True)
    st.markdown(
        """
        <div class="section-heading batch-section plain-section">
          <div>
            <h2>1. Batch ZIP</h2>
            <p>
              Upload one ZIP file containing one folder per product/application. Each product folder should contain exactly one JSON file with the application information, and 1-10 label images. You can put the product folders directly inside the ZIP, or inside one parent folder if that is how your computer creates the ZIP.
            </p>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    batch_example = """batch.zip
  old-tom-distillery/
    application.json
    label1.png
    label2.jpg

  cedar-hill-wine/
    data.json
    front.png
    back.png"""

    batch_example_with_wrapper = """batch.zip
  label-review-batch/
    old-tom-distillery/
      application.json
      label1.png
      label2.jpg

    cedar-hill-wine/
      data.json
      front.png
      back.png"""
    st.markdown(
        f"""
        <div class="zip-example-grid">
          {_zip_example_card("Format 1", "Product folders directly in the ZIP", batch_example)}
          {_zip_example_card("Format 2", "One parent folder inside the ZIP", batch_example_with_wrapper)}
        </div>
        """,
        unsafe_allow_html=True,
    )

    uploaded_zip = st.file_uploader("Upload batch ZIP", type=["zip"], key="batch_zip")

    parse_result = None
    if uploaded_zip is not None:
        parse_result = _parse_uploaded_batch_zip(uploaded_zip, config)
        rows = [_display_preview_row(row.model_dump(mode="json")) for row in preview_rows(parse_result.units)]
        st.subheader("Data Preview")
        st.markdown(f'<p class="preview-row-count">{len(rows)} rows</p>', unsafe_allow_html=True)
        _render_preview_table(rows)

        if parse_result.archive_error:
            st.error(parse_result.archive_error)

        valid_count = sum(1 for unit in parse_result.units if unit.is_valid)
        st.caption(f"{valid_count} ready to verify. {len(parse_result.units) - valid_count} cannot be verified until fixed.")

    st.markdown(
        """
        <div class="section-heading verify-section">
          <div>
            <h2>2. Run Verification</h2>
            <p>Start the batch review after the ZIP file is selected.</p>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if _centered_verify_button("Verify labels", "batch_verify_button"):
        if parse_result is None:
            st.session_state["batch_verify_notice"] = "Upload a batch ZIP file before running verification."
        else:
            st.session_state.pop("batch_verify_notice", None)
            _run_batch_review(config, parse_result.units)
    if st.session_state.get("batch_verify_notice"):
        st.error(st.session_state.pop("batch_verify_notice"))

    results = st.session_state.get("batch_results")
    if results is not None:
        _render_results(results, download_key="batch_download")


def _run_batch_review(config: AppConfig, units: Sequence[BatchReviewUnit]) -> None:
    results: List[VerificationResult] = []
    status_line = st.empty()
    progress = st.progress(0)
    total = max(len(units), 1)
    for index, unit in enumerate(units, start=1):
        status_line.write(f"Checking {unit.display_name}...")
        progress.progress((index - 1) / total)
        if not unit.is_valid or unit.application is None:
            results.append(invalid_unit_result(unit))
            continue
        results.append(verify_review_unit(unit.application, unit.images, unit.display_name, config))
    progress.progress(1.0)
    status_line.write("Batch verification complete.")
    st.session_state["batch_results"] = results


def _prepare_streamlit_images(uploaded_images: Sequence[object], config: AppConfig) -> List[PreparedImage]:
    files = []
    for uploaded in uploaded_images:
        name = getattr(uploaded, "name", "uploaded-image")
        content_type = getattr(uploaded, "type", None)
        data = uploaded.getvalue()
        files.append((name, data, content_type))
    return prepare_image_collection(
        files,
        max_images=config.max_images_per_unit,
        max_size_mb=config.max_image_mb,
        max_side_px=config.image_max_side_px,
        jpeg_quality=config.image_jpeg_quality,
    )


def _parse_uploaded_batch_zip(uploaded_zip: object, config: AppConfig):
    data = uploaded_zip.getvalue()
    signature_parts = [
        getattr(uploaded_zip, "name", ""),
        hashlib.sha256(data).hexdigest(),
        str(config.max_zip_mb),
        str(config.max_batch_units),
        str(config.max_images_per_unit),
        str(config.max_image_mb),
        str(config.image_max_side_px),
        str(config.image_jpeg_quality),
    ]
    signature = ":".join(signature_parts)
    cache = st.session_state.get("batch_zip_parse_cache")
    if cache and cache.get("signature") == signature:
        return cache["result"]

    st.session_state.pop("batch_preview_show_all", None)
    result = parse_batch_zip(getattr(uploaded_zip, "name", "batch.zip"), data, config)
    st.session_state["batch_zip_parse_cache"] = {"signature": signature, "result": result}
    return result


def _centered_verify_button(label: str, key: str) -> bool:
    left, middle, right = st.columns([1.35, 1, 1.35])
    with middle:
        return st.button(label, key=key)


def _render_results(results: Sequence[VerificationResult], download_key: str) -> None:
    st.subheader("Batch Results")
    counts = summary_counts(results)
    st.markdown(
        f"""
        <div class="summary-grid">
          <div class="summary-card"><span>Total</span><strong>{len(results)}</strong></div>
          <div class="summary-card pass"><span>Passed</span><strong>{counts["pass"]}</strong></div>
          <div class="summary-card needs"><span>Needs correction</span><strong>{counts["needs_correction"]}</strong></div>
          <div class="summary-card cannot"><span>Cannot verify</span><strong>{counts["cannot_verify"]}</strong></div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.download_button(
        "⇩\u00a0\u00a0Download batch result JSON",
        data=results_to_json(results),
        file_name="batch-verification-results.json",
        mime="application/json",
        key=download_key,
    )

    st.markdown(
        '<div class="review-list">' + "".join(_review_card(result) for result in results) + "</div>",
        unsafe_allow_html=True,
    )


def _render_result(result: VerificationResult, download_key: str) -> None:
    st.subheader("Result")
    _render_status_summary(result)
    st.download_button(
        "⇩\u00a0\u00a0Download result JSON",
        data=single_result_to_json(result),
        file_name="verification-result.json",
        mime="application/json",
        key=download_key,
    )
    _render_result_body(result)


def _render_result_body(result: VerificationResult) -> None:
    st.write(result.summary)
    cards = "\n".join(_check_card(check) for check in result.checks)
    st.markdown(f'<div class="checks-list">{cards}</div>', unsafe_allow_html=True)


def _render_status_summary(result: VerificationResult) -> None:
    css_class = _status_class(result.overall_status)
    st.markdown(
        f"""
        <div class="status-box {css_class}">
          <span>{_status_label(result.overall_status)}</span>
          <strong>{_html(result.summary)}</strong>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _display_preview_row(row: dict) -> dict:
    return {
        "Product": row["review_unit"],
        "JSON": row["json_status"],
        "Images": row["image_count"],
        "Status": "Ready" if row["validation_status"] == "ready" else "Cannot verify",
        "Details": row["validation_details"],
    }


def _zip_example_card(label: str, description: str, tree: str) -> str:
    return (
        '<div class="zip-example">'
        f'<div class="zip-example-title"><span>{_html(label)}</span><strong>{_html(description)}</strong></div>'
        f'<div class="zip-example-tree">{_zip_tree_html(tree)}</div>'
        "</div>"
    )


def _zip_tree_html(tree: str) -> str:
    return "<br>".join(_html(line).replace(" ", "&nbsp;") for line in tree.splitlines())


def _render_preview_table(rows: Sequence[dict]) -> None:
    headers = ["Product", "JSON", "Images", "Status", "Details"]
    show_all_key = "batch_preview_show_all"
    show_all = bool(st.session_state.get(show_all_key, False))
    visible_rows = _preview_visible_rows(rows, show_all)
    header_html = "".join(f"<th>{_html(header)}</th>" for header in headers)
    body_html = "".join(_preview_table_row(row, headers) for row in visible_rows)
    st.markdown(
        f"""
        <div class="preview-table-wrap">
          <table class="preview-table">
            <thead><tr>{header_html}</tr></thead>
            <tbody>{body_html}</tbody>
          </table>
        </div>
        """,
        unsafe_allow_html=True,
    )

    total_rows = len(rows)
    if total_rows > PREVIEW_ROW_LIMIT:
        toggle_state_class = "preview-toggle-expanded" if show_all else "preview-toggle-collapsed"
        st.markdown(f'<div class="preview-toggle-anchor {toggle_state_class}"></div>', unsafe_allow_html=True)
        _, toggle_col = st.columns([1, 0.08])
        with toggle_col:
            help_text = "Show first 10 rows" if show_all else f"Show all {total_rows} rows"
            if st.button(" ", key="batch_preview_toggle", help=help_text):
                st.session_state[show_all_key] = not show_all
                _rerun()


def _preview_visible_rows(rows: Sequence[dict], show_all: bool) -> Sequence[dict]:
    if show_all or len(rows) <= PREVIEW_ROW_LIMIT:
        return rows
    return rows[:PREVIEW_ROW_LIMIT]


def _preview_table_row(row: dict, headers: Sequence[str]) -> str:
    cells = []
    for header in headers:
        value = row.get(header, "")
        if header == "Status":
            status_class = "status-ready" if str(value).lower() == "ready" else "status-warning"
            cells.append(f'<td><span class="preview-status {status_class}">{_html(value)}</span></td>')
        else:
            cells.append(f"<td>{_html(value)}</td>")
    return "<tr>" + "".join(cells) + "</tr>"


def _check_card(check: object) -> str:
    status_class = _status_class(check.status)
    rows = [
        ("Application value", check.application_value),
        ("Label value", check.label_value),
        ("Evidence image", check.evidence_image),
        ("Evidence text", check.evidence_text),
        ("Reason", check.reason),
    ]
    details = "".join(
        f"<p><span>{_html(label)}:</span> {_html(value)}</p>"
        for label, value in rows
        if value is not None
    )
    return (
        f'<details class="check-card {status_class}" {"open" if check.status != "pass" else ""}>'
        f"<summary><strong>{_html(_field_label(check.field))}</strong>"
        f'<span class="badge {status_class}">{_html(_status_label(check.status))}</span></summary>'
        f'<div class="check-details">{details}</div>'
        "</details>"
    )


def _review_card(result: VerificationResult) -> str:
    status_class = _status_class(result.overall_status)
    return (
        f'<details class="review-card {status_class}" {"open" if result.overall_status != "pass" else ""}>'
        f"<summary><strong>{_html(result.review_unit)}</strong>"
        f'<span class="badge {status_class}">{_html(_status_label(result.overall_status))}</span></summary>'
        f'<div class="review-details"><p>{_html(result.summary)}</p>'
        f'<div class="checks-list">{"".join(_check_card(check) for check in result.checks)}</div></div>'
        "</details>"
    )
def _field_label(field: str) -> str:
    labels = {
        "brand_name": "Brand name",
        "class_type": "Class / type",
        "alcohol_content": "Alcohol content",
        "net_contents": "Net contents",
        "government_warning": "Government warning",
        "name_and_address": "Name and address",
        "country_of_origin": "Country of origin",
        "review_unit": "Review unit",
    }
    return labels.get(field, field.replace("_", " ").capitalize())


def _status_label(status: str) -> str:
    return {
        "pass": "Passed",
        "needs_correction": "Needs correction",
        "cannot_verify": "Cannot verify",
    }.get(status, status.replace("_", " ").capitalize())


def _status_class(status: str) -> str:
    return {
        "pass": "status-pass",
        "needs_correction": "status-needs",
        "cannot_verify": "status-cannot",
    }[status]


def _html(value: object) -> str:
    return html.escape(str(value))


def _inject_css() -> None:
    st.markdown(
        """
        <link href="https://fonts.googleapis.com/css2?family=Material+Symbols+Rounded:opsz,wght,FILL,GRAD@24,400,0,0" rel="stylesheet">
        <style>
        :root {
            --alv-mode-accent: #6ea8d8;
            --alv-mode-accent-hover: #86b7df;
            --alv-mode-accent-text: #06131f;
        }
        .css-18e3th9, .block-container {
            padding-top: 1.8rem !important;
            max-width: 1180px;
        }
        .element-container,
        .stMarkdown,
        .stCaptionContainer,
        div[data-testid="stMarkdownContainer"],
        div[data-testid="stMarkdownContainer"] > *,
        div[data-testid="stCaptionContainer"],
        div[data-testid="stCaptionContainer"] > * {
            max-width: none !important;
            width: 100%;
        }
        .stMarkdown p,
        .stMarkdown h1,
        .stMarkdown h2,
        .stMarkdown h3,
        .stMarkdown h4,
        .stMarkdown li,
        div[data-testid="stMarkdownContainer"] p,
        div[data-testid="stMarkdownContainer"] h1,
        div[data-testid="stMarkdownContainer"] h2,
        div[data-testid="stMarkdownContainer"] h3,
        div[data-testid="stMarkdownContainer"] h4,
        div[data-testid="stMarkdownContainer"] li {
            max-width: none !important;
        }
        .stMarkdown h1 a, .stMarkdown h2 a, .stMarkdown h3 a,
        .stMarkdown [data-testid="StyledLinkIconContainer"] a,
        a.anchor-link, a.header-link, a[href^="#"] {
            display: none !important;
            visibility: hidden !important;
            width: 0 !important;
            height: 0 !important;
            margin: 0 !important;
            padding: 0 !important;
        }
        section[data-testid="stSidebar"] {
            display: none !important;
        }
        div[role="radiogroup"] {
            justify-content: center;
            gap: 1.6rem;
        }
        div[role="radiogroup"] label {
            display: inline-flex;
            align-items: center;
            width: auto;
            min-width: 0;
        }
        div[role="radiogroup"] label > div:first-child {
            display: none;
        }
        div[role="radiogroup"] label p {
            color: rgba(255,255,255,0.84) !important;
            font-size: 1rem;
            font-weight: 650;
            white-space: nowrap;
            margin: 0;
        }
        div[role="radiogroup"] label[data-checked="true"] p,
        div[role="radiogroup"] label:has(input:checked) p {
            color: var(--alv-mode-accent) !important;
        }
        .mode-rule {
            position: relative;
            height: 2px;
            background: rgba(255,255,255,0.14);
            margin: 0.45rem 0 1.15rem 0;
        }
        .mode-rule::after {
            content: "";
            position: absolute;
            top: 0;
            bottom: 0;
            left: 0;
            width: 50%;
            background: var(--alv-mode-accent);
        }
        .mode-rule-batch::after {
            left: 50%;
        }
        .hero {
            padding: 0.4rem 0 1rem 0;
        }
        .hero h1 {
            margin: 0 0 0.55rem 0;
            font-size: 2.75rem;
            line-height: 1.05;
            letter-spacing: 0;
        }
        .hero p, .mode-intro, .section-heading p, .field-note {
            color: rgba(255,255,255,0.72);
            font-size: 1.02rem;
            line-height: 1.55;
            max-width: none;
        }
        .field-note {
            max-width: none;
            white-space: nowrap;
            margin: 1rem 0 0.65rem 0;
        }
        .section-heading {
            border-left: 4px solid var(--alv-mode-accent);
            background: rgba(110, 168, 216, 0.08);
            padding: 1rem 1.15rem;
            border-radius: 6px;
            margin: 2.4rem 0 0.9rem 0;
        }
        .image-section,
        .verify-section,
        .batch-section,
        .plain-section {
            border-left-color: var(--alv-mode-accent);
            background: rgba(110, 168, 216, 0.08);
        }
        .section-heading h2 {
            margin: 0 0 0.25rem 0;
            font-size: 1.45rem;
            letter-spacing: 0;
        }
        .section-heading p {
            margin: 0;
        }
        .mode-intro {
            margin-bottom: 0.8rem;
            color: var(--alv-mode-accent) !important;
            text-align: center;
        }
        .json-entry-panel {
            margin-top: 0.5rem;
        }
        .reset-button-spacer {
            height: 2.1rem;
        }
        div[data-testid="stVerticalBlock"] > div:has(.reset-button-spacer) + div .stButton > button {
            background: var(--alv-mode-accent);
            border-color: rgba(182, 236, 255, 0.72);
            color: var(--alv-mode-accent-text) !important;
        }
        div[data-testid="stVerticalBlock"] > div:has(.reset-button-spacer) + div .stButton > button:hover {
            background: var(--alv-mode-accent-hover) !important;
            border-color: rgba(220, 247, 255, 0.9) !important;
            color: var(--alv-mode-accent-text) !important;
        }
        div[data-testid="stVerticalBlock"] > div:has(.reset-button-spacer) + div .stButton > button:hover * {
            color: var(--alv-mode-accent-text) !important;
        }
        .zip-example-grid {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 1rem;
            margin: 1rem 0 1.4rem 0;
        }
        .zip-example {
            padding: 1.1rem 1.25rem;
            border-radius: 6px;
            background: rgba(255,255,255,0.055);
            color: rgba(255,255,255,0.92);
            overflow-x: auto;
        }
        .zip-example-title {
            margin-bottom: 0.85rem;
        }
        .zip-example-title span {
            display: block;
            color: var(--alv-mode-accent);
            font-size: 0.78rem;
            font-weight: 800;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            margin-bottom: 0.2rem;
        }
        .zip-example-title strong {
            display: block;
            color: rgba(255,255,255,0.9);
            font-size: 0.94rem;
            line-height: 1.35;
        }
        .zip-example-tree {
            color: rgba(255,255,255,0.9);
            font-family: "Source Code Pro", Consolas, "Courier New", monospace;
            font-size: 0.9rem;
            line-height: 1.5;
            white-space: nowrap;
        }
        .preview-table-wrap {
            overflow-x: auto;
            margin: 0.75rem 0 1.25rem 0;
        }
        .preview-row-count {
            color: rgba(255,255,255,0.56);
            font-size: 0.95rem;
            margin: -0.25rem 0 0.45rem 0;
        }
        .preview-table {
            width: 100%;
            border-collapse: collapse;
            border: 1px solid rgba(255,255,255,0.14);
            background: rgba(255,255,255,0.025);
            font-size: 0.95rem;
        }
        .preview-table th,
        .preview-table td {
            border-bottom: 1px solid rgba(255,255,255,0.10);
            border-right: 1px solid rgba(255,255,255,0.10);
            padding: 0.75rem 0.85rem;
            text-align: left;
            vertical-align: top;
        }
        .preview-table th {
            color: rgba(255,255,255,0.72);
            font-weight: 700;
            background: rgba(255,255,255,0.035);
        }
        .preview-table td:last-child {
            min-width: 18rem;
        }
        .preview-table tr:last-child td {
            border-bottom: 0;
        }
        .preview-toggle-anchor {
            height: 0;
            margin: -1rem 0 0 0;
        }
        div:has(.preview-toggle-anchor) + div,
        div[data-testid="stVerticalBlock"] > div:has(.preview-toggle-anchor) + div {
            margin-top: -0.85rem !important;
        }
        div:has(.preview-toggle-anchor) + div .stButton,
        div[data-testid="stVerticalBlock"] > div:has(.preview-toggle-anchor) + div .stButton {
            display: flex;
            justify-content: flex-end;
        }
        div:has(.preview-toggle-anchor) + div button,
        div:has(.preview-toggle-anchor) + div button[kind],
        div:has(.preview-toggle-anchor) + div .stButton button,
        div[data-testid="stVerticalBlock"] > div:has(.preview-toggle-anchor) + div .stButton > button,
        .st-key-batch_preview_toggle button,
        .st-key-batch_preview_toggle button[kind] {
            background: transparent !important;
            background-color: transparent !important;
            border: 0 none transparent !important;
            color: rgba(255,255,255,0.42) !important;
            min-width: 1.1rem !important;
            width: 1.1rem !important;
            min-height: 1.1rem !important;
            height: 1.1rem !important;
            padding: 0 !important;
            box-shadow: none !important;
            font-size: 1.2rem !important;
            font-weight: 500 !important;
            line-height: 1 !important;
            outline: none !important;
        }
        div:has(.preview-toggle-anchor) + div button *,
        div:has(.preview-toggle-anchor) + div .stButton button *,
        .st-key-batch_preview_toggle button * {
            color: transparent !important;
            font-size: 0 !important;
            line-height: 0 !important;
            opacity: 0 !important;
            width: 0 !important;
            max-width: 0 !important;
            overflow: hidden !important;
        }
        div:has(.preview-toggle-anchor) + div button::before,
        div:has(.preview-toggle-anchor) + div .stButton button::before,
        .st-key-batch_preview_toggle button::before {
            content: "keyboard_arrow_down";
            font-family: "Material Symbols Rounded";
            font-weight: normal;
            font-style: normal;
            font-size: 1.6rem;
            line-height: 1;
            letter-spacing: normal;
            text-transform: none;
            display: inline-block;
            white-space: nowrap;
            overflow-wrap: normal;
            direction: ltr;
            font-feature-settings: "liga";
            -webkit-font-feature-settings: "liga";
            -webkit-font-smoothing: antialiased;
            color: rgba(255,255,255,0.46);
            display: block;
            width: 1.6rem;
            height: 1.6rem;
            margin: 0 auto;
        }
        div:has(.preview-toggle-collapsed) + div button::before,
        div:has(.preview-toggle-collapsed) + div .stButton button::before {
            content: "keyboard_arrow_down";
            margin-top: -0.25rem;
        }
        div:has(.preview-toggle-expanded) + div button::before,
        div:has(.preview-toggle-expanded) + div .stButton button::before {
            content: "keyboard_arrow_up";
            margin-top: -0.2rem;
        }
        div:has(.preview-toggle-anchor) + div button:hover,
        div:has(.preview-toggle-anchor) + div button[kind]:hover,
        div:has(.preview-toggle-anchor) + div .stButton button:hover,
        div[data-testid="stVerticalBlock"] > div:has(.preview-toggle-anchor) + div .stButton > button:hover,
        .st-key-batch_preview_toggle button:hover,
        .st-key-batch_preview_toggle button[kind]:hover {
            background: transparent !important;
            background-color: transparent !important;
            border: 0 none transparent !important;
            color: rgba(255,255,255,0.72) !important;
            box-shadow: none !important;
        }
        div:has(.preview-toggle-anchor) + div button:hover *,
        div:has(.preview-toggle-anchor) + div .stButton button:hover *,
        .st-key-batch_preview_toggle button:hover * {
            color: transparent !important;
            opacity: 0 !important;
        }
        div:has(.preview-toggle-anchor) + div button:hover::before,
        div:has(.preview-toggle-anchor) + div .stButton button:hover::before,
        .st-key-batch_preview_toggle button:hover::before {
            color: rgba(255,255,255,0.72);
        }
        .preview-status {
            font-weight: 800;
            white-space: nowrap;
        }
        .preview-status.status-ready {
            color: #86efac;
        }
        .preview-status.status-warning {
            color: #fcd34d;
        }
        .stFileUploader section {
            min-height: 86px !important;
            padding: 0.75rem !important;
            border-radius: 6px !important;
        }
        .stFileUploader label {
            font-weight: 650 !important;
        }
        .stButton > button {
            border-radius: 6px;
            border: 1px solid rgba(255,255,255,0.22);
            padding: 0.4rem 0.85rem;
            min-height: 2.25rem;
            font-weight: 650;
            background: #5f789f;
            color: white;
            font-size: 0.92rem;
        }
        .stButton > button:hover {
            background: #6f90c3 !important;
            border-color: rgba(174, 204, 244, 0.6) !important;
            color: white !important;
        }
        .stButton > button:hover * {
            color: white !important;
        }
        .stDownloadButton > button {
            border-radius: 6px;
            border: 1px solid rgba(182, 236, 255, 0.72) !important;
            padding: 0.4rem 0.85rem;
            min-height: 2.25rem;
            font-weight: 650;
            background: var(--alv-mode-accent) !important;
            color: var(--alv-mode-accent-text) !important;
            font-size: 0.92rem;
        }
        .stDownloadButton > button:hover {
            background: var(--alv-mode-accent-hover) !important;
            border-color: rgba(220, 247, 255, 0.9) !important;
            color: var(--alv-mode-accent-text) !important;
        }
        .stDownloadButton > button:hover * {
            color: var(--alv-mode-accent-text) !important;
        }
        div[data-testid="stVerticalBlock"] > div:has(.verify-section) + div {
            display: flex;
            justify-content: center;
        }
        div[data-testid="stVerticalBlock"] > div:has(.verify-section) + div .stButton > button {
            background: #16a34a;
            border-color: #22c55e;
            color: white;
            font-size: 1.08rem;
            min-width: 12rem;
            min-height: 3rem;
        }
        div[data-testid="stVerticalBlock"] > div:has(.verify-section) + div .stButton > button:hover {
            background: #22c55e !important;
            border-color: #4ade80 !important;
            color: white !important;
        }
        div[data-testid="stVerticalBlock"] > div:has(.verify-section) + div .stButton > button:hover * {
            color: white !important;
        }
        .stTextArea label {
            display: none !important;
        }
        .stTextArea textarea {
            margin-top: 0 !important;
            border-radius: 6px !important;
        }
        div[data-testid="stFileUploaderFile"],
        div[data-testid="stFileUploaderFileList"] {
            display: none !important;
        }
        .status-box {
            border: 1px solid rgba(255,255,255,0.14);
            border-left-width: 6px;
            border-radius: 6px;
            padding: 1rem 1.1rem;
            margin: 0.5rem 0 1rem 0;
            background: rgba(255,255,255,0.055);
        }
        .status-box span {
            display: inline-block;
            margin-bottom: 0.35rem;
            font-weight: 800;
            text-transform: uppercase;
            font-size: 0.82rem;
            letter-spacing: 0.04em;
        }
        .status-pass { border-color: #22c55e !important; }
        .status-needs { border-color: #ef4444 !important; }
        .status-cannot { border-color: #f59e0b !important; }
        .summary-grid {
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: 0.75rem;
            margin: 0.75rem 0 1rem 0;
        }
        .summary-card {
            border: 1px solid rgba(255,255,255,0.14);
            border-radius: 6px;
            padding: 0.9rem;
            background: rgba(255,255,255,0.05);
        }
        .summary-card span {
            display: block;
            color: rgba(255,255,255,0.68);
            margin-bottom: 0.25rem;
        }
        .summary-card strong {
            font-size: 1.65rem;
        }
        .summary-card.pass { border-left: 5px solid #22c55e; }
        .summary-card.needs { border-left: 5px solid #ef4444; }
        .summary-card.cannot { border-left: 5px solid #f59e0b; }
        .checks-list {
            display: grid;
            gap: 0.75rem;
            margin-top: 1rem;
        }
        .review-list {
            display: grid;
            gap: 0.85rem;
            margin-top: 1rem;
        }
        .review-card {
            border: 1px solid rgba(255,255,255,0.18);
            border-left-width: 5px;
            border-radius: 6px;
            background: rgba(255,255,255,0.035);
        }
        .review-card > summary {
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 1rem;
            padding: 0.95rem 1rem;
            list-style: none;
        }
        .review-card > summary::-webkit-details-marker {
            display: none;
        }
        .review-card > summary strong {
            font-size: 1.05rem;
        }
        .review-card.status-pass > summary strong {
            color: #86efac;
        }
        .review-card.status-needs > summary strong {
            color: #fca5a5;
        }
        .review-card.status-cannot > summary strong {
            color: #fcd34d;
        }
        .review-details {
            border-top: 1px solid rgba(255,255,255,0.10);
            padding: 0.85rem 1rem 1rem 1rem;
        }
        .check-card {
            border: 1px solid rgba(255,255,255,0.16);
            border-left-width: 5px;
            border-radius: 6px;
            background: rgba(255,255,255,0.035);
            padding: 0;
        }
        .check-card summary {
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 1rem;
            padding: 0.95rem 1rem;
            list-style: none;
        }
        .check-card summary::-webkit-details-marker {
            display: none;
        }
        .badge {
            display: inline-flex;
            align-items: center;
            border-radius: 999px;
            padding: 0.22rem 0.65rem;
            font-size: 0.84rem;
            font-weight: 800;
            white-space: nowrap;
        }
        .badge.status-pass {
            background: rgba(34,197,94,0.16);
            color: #86efac;
        }
        .badge.status-needs {
            background: rgba(239,68,68,0.18);
            color: #fca5a5;
        }
        .badge.status-cannot {
            background: rgba(245,158,11,0.18);
            color: #fcd34d;
        }
        .check-details {
            border-top: 1px solid rgba(255,255,255,0.10);
            padding: 0.85rem 1rem 1rem 1rem;
        }
        .check-details p {
            margin: 0.35rem 0;
            line-height: 1.55;
        }
        .check-details span {
            color: rgba(255,255,255,0.67);
            font-weight: 700;
        }
        @media (max-width: 760px) {
            .hero h1 {
                font-size: 2.05rem;
            }
            .summary-grid {
                grid-template-columns: repeat(2, minmax(0, 1fr));
            }
            .check-card summary {
                align-items: flex-start;
                flex-direction: column;
            }
            .field-note {
                white-space: normal;
            }
            .zip-example-grid {
                grid-template-columns: 1fr;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _rerun() -> None:
    if hasattr(st, "rerun"):
        st.rerun()
    else:
        st.experimental_rerun()
