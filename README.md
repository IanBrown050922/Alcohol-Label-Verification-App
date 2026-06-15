# Alcohol Label Verifier

AI-assisted review for alcohol label applications.

The app compares submitted alcohol label artwork against structured application data and returns a field-by-field verification report. It is built to help reviewers quickly answer the routine matching questions that appear in label review:

* Does the brand name match?
* Does the class/type match?
* Does the alcohol content match?
* Does the net contents statement match?
* Is the required government warning present?
* Are optional origin/address fields consistent when provided?

The app is a verification assistant. It does not connect to COLAs Online, does not use government IDs, and does not make legal approval decisions.

## Live App

Add deployed URL here before submission:

```text
https://your-deployed-app-url
```

## Core Concept

A review unit is one product submission:

```text
one application JSON
+ 1-10 label images
```

All images in a review unit are treated as one unordered evidence set. Users do not need to name files in a specific way, upload them in a specific order, or classify them as front, back, neck, strip, or brand labels.

This keeps the workflow simple: the user provides the application data and the related label images, and the app checks whether the label evidence matches the application.

## Design Decisions

The app is standalone because the review flow should be testable without access to COLAs Online, internal IDs, or existing export formats.

The input format is intentionally simple. Single Review uses one JSON editor and one image uploader. Batch Review uses a ZIP file where each folder is one review unit. This avoids requiring users to manually map images to applications in a large batch.

The app uses OpenAI Vision for this version because the difficult part is reading and comparing unordered label images quickly. A separate OCR pipeline or self-hosted model could be added later, but this version prioritizes a working, fast, evidence-backed review flow.

Uploaded files are processed only for the active verification run and are not permanently stored by the app.

## What It Checks

For each review unit, the app checks:

* `brand_name`
* `class_type`
* `alcohol_content`
* `net_contents`
* `government_warning`
* `name_and_address`, when supplied
* `country_of_origin`, when supplied

Results are returned as:

```text
pass
needs_correction
cannot_verify
```

Each result includes field-level evidence text, source image filename when available, and a short explanation.

The app checks for this U.S. government warning text:

```text
GOVERNMENT WARNING: (1) According to the Surgeon General, women should not drink alcoholic beverages during pregnancy because of the risk of birth defects. (2) Consumption of alcoholic beverages impairs your ability to drive a car or operate machinery, and may cause health problems.
```

It checks the warning text and `GOVERNMENT WARNING` heading. It does not verify font size, boldness, contrast, character density, or physical label placement.

## Quick Start

### macOS/Linux

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
streamlit run app.py
```

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
streamlit run app.py
```

Before running live verification, edit `.env` or `local.env`:

```env
OPENAI_API_KEY=your-openai-api-key
OPENAI_MODEL=gpt-5.4-nano
```

The repository does not include an OpenAI API key. To run live verification locally, provide your own key in `.env`, `local.env`, your shell environment, or Streamlit secrets.

`gpt-5.4-nano` is the default configured model. If it is unavailable on your account or label-reading quality is not sufficient, use:

```env
OPENAI_MODEL=gpt-5.4-mini
```

Optional model compatibility check:

```bash
python scripts/smoke_openai.py
```

The smoke test sends two generated images to the configured model and verifies that the same Pydantic structured-output path used by the app works. It is not run before every review.

## Application JSON

The Single Review editor starts with this example structure:

```json
{
  "product_type": "distilled_spirits",
  "brand_name": "OLD TOM DISTILLERY",
  "class_type": "Kentucky Straight Bourbon Whiskey",
  "alcohol_content": "45% Alc./Vol. (90 Proof)",
  "net_contents": "750 mL",
  "name_and_address": "Bottled by Old Tom Distillery, Frankfort, KY",
  "country_of_origin": null
}
```

Required fields:

```text
product_type
brand_name
class_type
alcohol_content
net_contents
```

Optional fields:

```text
name_and_address
country_of_origin
```

Allowed `product_type` values:

```text
distilled_spirits
wine
malt_beverage
```

The app also normalizes a few simple aliases, such as `spirits` to `distilled_spirits` and `beer` to `malt_beverage`.

## Single Review

Use Single Review for one product.

1. Edit the example JSON or load a `.json` file into the editor.
2. Upload 1-10 label images.
3. Click **Verify label**.
4. Review the field-level results.
5. Download the result JSON if needed.

Uploading a valid JSON file replaces the editor content. Invalid JSON shows an error and preserves the current editor content.

Supported image extensions:

```text
.jpg
.jpeg
.jpe
.png
```

## Batch Review

Use Batch Review for multiple products.

Batch Review accepts one ZIP file. Each product folder must contain exactly one JSON file and 1-10 supported image files.

Accepted format:

```text
batch.zip
  old-tom-distillery/
    application.json
    label1.png
    label2.jpg

  cedar-hill-wine/
    data.json
    front.png
    back.png
```

Also accepted:

```text
batch.zip
  label-review-batch/
    old-tom-distillery/
      application.json
      label1.png
      label2.jpg

    cedar-hill-wine/
      data.json
      front.png
      back.png
```

The parent folder in the second format is treated as a wrapper. Product folder names are display labels only.

Batch Review shows a Data Preview table before model calls. It includes product, JSON status, image count, validation status, and details. If there are more than 10 rows, the preview initially shows 10 and can be expanded.

Invalid units are included in final results as `cannot_verify`; they are not silently skipped.

## Data Handling

Uploaded JSON and images are processed only for the active verification run and are not permanently stored by the app.

The app avoids persistent upload directories and does not log uploaded JSON, image content, base64 image data, full prompts, full responses, or API keys.

Live verification sends the application JSON and label images to OpenAI for inference. Use only data appropriate for your OpenAI account, organization policy, and deployment environment.

## Configuration

Environment variables:

```text
OPENAI_API_KEY              required for live OpenAI calls
OPENAI_MODEL                default: gpt-5.4-nano
OPENAI_TIMEOUT_SECONDS      default: 30
MAX_IMAGES_PER_UNIT         default: 10
MAX_IMAGE_MB                default: 10
MAX_ZIP_MB                  default: 100
MAX_BATCH_UNITS             default: 50
IMAGE_MAX_SIDE_PX           default: 1600
IMAGE_JPEG_QUALITY          default: 82
ALV_MOCK_OPENAI             default: false
```

`ALV_MOCK_OPENAI=true` is for local UI testing only. It does not inspect label images and should not be used to judge verification quality.

## OpenAI Integration

The frontend never calls OpenAI directly. Server-side Python sends each review unit through the OpenAI Responses API using:

* one concise text prompt
* one application JSON object
* 1-10 `input_image` parts
* Pydantic structured output parsing via `client.responses.parse(..., text_format=VerificationResult)`

The app validates and normalizes model output before rendering it. If the API call fails, times out, or returns malformed output, that review unit becomes `cannot_verify`.

## Deployment

The app is designed for Streamlit Community Cloud or similar Python hosting.

For Streamlit Community Cloud, configure secrets in the app settings:

```toml
OPENAI_API_KEY = "your-openai-api-key"
OPENAI_MODEL = "gpt-5.4-nano"
```

Do not commit `.env`, `local.env`, `.streamlit/secrets.toml`, API keys, cache files, raw data, project instructions, or generated Python cache directories.

`.streamlit/config.toml` is safe to commit; it only controls the Streamlit theme.

## Sample Data

Single Review samples:

```text
sample_data/single/passing/
sample_data/single/mismatch/
sample_data/single/missing-field/
```

Batch samples:

```text
sample_data/batch/sample_batch.zip
sample_data/batch/sample_batch_2.zip
```

To regenerate synthetic samples:

```bash
python scripts/generate_sample_data.py
```

## Tests

Run:

```bash
pytest
```

Tests cover non-model logic:

* JSON validation
* image validation
* batch ZIP parsing
* wrapper-folder ZIPs
* ZIP traversal rejection
* status aggregation
* malformed model output handling
* API error conversion
* UI rendering helpers

Live OpenAI calls are not required for tests.

## Project Structure

```text
app.py                         Streamlit entry point
src/config.py                  Runtime settings and constants
src/schemas.py                 Pydantic data schemas
src/validation.py              Application JSON validation
src/image_utils.py             Image validation and resizing
src/batch.py                   Batch ZIP parsing and preflight validation
src/openai_verifier.py         OpenAI integration and mock path
src/results.py                 Result validation and aggregation
src/ui.py                      Streamlit UI
scripts/generate_sample_data.py
scripts/smoke_openai.py
tests/
sample_data/
```

## Limitations

* Not a legal determination tool.
* Does not perform full beverage-law compliance review.
* Does not connect to COLAs Online.
* Does not infer image roles such as front, back, neck, or strip label.
* No OCR fallback, local VLM runtime, vLLM, llama.cpp, or Ollama.
* Does not verify warning typography, contrast, character density, or physical placement.
* Very small, distorted, low-contrast, or partially visible text may return `cannot_verify`.

## Troubleshooting

**Missing API key:** Set `OPENAI_API_KEY` in `.env`, `local.env`, shell environment, or Streamlit secrets.

**Model unavailable or schema errors:** Try `OPENAI_MODEL=gpt-5.4-mini`, then run `python scripts/smoke_openai.py`.

**OpenAI dashboard shows zero usage:** Confirm the API key belongs to the selected OpenAI project, check the project filter/date range, and allow for dashboard delay. The app logs HTTP status lines when OpenAI calls are made, but does not log prompts or file contents.

**Image rejected:** Use `.jpg`, `.jpeg`, `.jpe`, or `.png`; keep files under `MAX_IMAGE_MB`.

**Batch ZIP rejected:** Use one product folder per review unit, either directly in the ZIP or under one parent wrapper folder. Avoid root-level files, deeper nesting, unsupported files, and unsafe paths.

**Local app appears in light mode:** Restart Streamlit after changes to `.streamlit/config.toml`.

## Research Sources

* OpenAI Responses API: https://platform.openai.com/docs/api-reference/responses
* OpenAI images and vision guide: https://platform.openai.com/docs/guides/images
* OpenAI structured outputs guide: https://platform.openai.com/docs/guides/structured-outputs
* eCFR 27 CFR 16.21 government warning: https://www.ecfr.gov/current/title-27/chapter-I/subchapter-A/part-16/section-16.21
* eCFR 27 CFR 4.32 wine mandatory label information: https://www.ecfr.gov/current/title-27/chapter-I/subchapter-A/part-4/section-4.32
* eCFR 27 CFR 5.63 distilled spirits mandatory label information: https://www.ecfr.gov/current/title-27/chapter-I/subchapter-A/part-5/section-5.63
* eCFR 27 CFR 7.63 malt beverage mandatory label information: https://www.ecfr.gov/current/title-27/chapter-I/subchapter-A/part-7/section-7.63
* eCFR 19 CFR 134.11 country-of-origin marking: https://www.ecfr.gov/current/title-19/chapter-I/part-134/subpart-B/section-134.11
* Streamlit Community Cloud deployment: https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app
* Streamlit secrets: https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management
* Streamlit file uploader: https://docs.streamlit.io/develop/api-reference/widgets/st.file_uploader
