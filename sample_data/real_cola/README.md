# Real COLA Sample Data

This directory contains app-ready sample review units generated from the COLA Cloud sample pack.
The raw extracted CSV package lives under `raw_data/cola-sample-pack-v1` and is based on public TTB COLA records.

The generator reads `cola.csv` and `cola_image.csv`, links records with `cola.TTB_ID -> cola_image.TTB_ID`, downloads label images from:

```text
https://dyuie4zgfxmt6.cloudfront.net/{TTB_IMAGE_ID}.webp
```

Source images are WebP. The app supports `.jpg`, `.jpeg`, `.jpe`, and `.png`, so the generator validates each downloaded image with Pillow and saves it as JPEG.

## What Was Generated

- Passing real records: 20
- Mismatch records: 5
- Cannot-verify records: 3
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
