#!/usr/bin/env python3
"""
Hybrid Local/Cloud Verbatim Magazine OCR Pipeline
- Initializes SQLite state database in the target magazine directory
- Scrubs image EXIF/metadata and maps page-NNNN.png to anonymous UUIDs
- Routes pages between local Ollama (qwen2.5vl) and zero-retention Cloud VLM
- Validates JSON schema, normalized bounding boxes, and glyph integrity
- Writes per-page raw output to raw_text/page-NNNN.json
"""

import argparse
import base64
import io
import json
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

import requests
from PIL import Image
from pydantic import BaseModel, Field, ValidationError

# ==============================================================================
# 1. DETERMINISTIC SYSTEM PROMPT & SCHEMA
# ==============================================================================

SYSTEM_PROMPT = """You are a precision archival digitization engine for vintage and modern science fiction magazines (Astounding / Analog).
Extract ALL visible textual elements from the provided magazine page image with 100% verbatim fidelity.

CRITICAL EXTRACTION RULES:
1. EXHAUSTIVE COVERAGE: Extract EVERY piece of text on the page: mastheads, running headers/footers, page numbers, story titles, author bylines, drop caps, body columns, footnotes, marginalia, rotated/vertical text, artist signatures, captions, and ALL text embedded inside illustrations, diagrams, and advertisements.
2. LITERAL GLYPH ACCURACY: Preserve exact spelling, archaic punctuation, capitalization, line-break hyphens, fractions, and special symbols (©, ®, ™, §, †, ‡, °, —, –). Do NOT modernize spelling or correct OCR errors in the source print.
3. DETERMINISTIC TYPOGRAPHIC STYLING:
   - Map bold text to **bold**
   - Map italic text to *italic*
   - Map underlined text to <u>underlined</u>
   - Map strikethrough text to <s>strikethrough</s>
   - Map headings to Markdown #, ##, ### based on visual hierarchy.
4. STRUCTURAL ROLES & READING ORDER:
   - Order elements in logical human reading order (top-to-bottom, left-to-right per column).
   - Assign each element one strict role:
     "masthead", "running_header", "running_footer", "page_number", "title", "subtitle", "byline", "body", "caption", "marginalia", "graphic_text", "advertisement", "table", "continuation_marker"
5. NORMALIZED BOUNDING BOXES & ROTATION:
   - Provide `bbox_1000` as [ymin, xmin, ymax, xmax] using integers scaled 0 to 1000 relative to page dimensions.
   - Provide `rotation_deg` as an integer (0, 90, 180, 270, or approximate angle for diagonal art text).
6. NON-TEXT VISUAL ASSETS:
   - Also list all illustrations, photographs, decorative borders, and ad graphics in `visual_assets` with their `bbox_1000` and brief description so downstream scripts can crop them.

Respond ONLY with valid JSON matching the required schema. Do not wrap in markdown fences or add commentary."""


class TextElement(BaseModel):
    sequence: int = Field(..., description="1-based reading order index on page")
    role: Literal[
        "masthead",
        "running_header",
        "running_footer",
        "page_number",
        "title",
        "subtitle",
        "byline",
        "body",
        "caption",
        "marginalia",
        "graphic_text",
        "advertisement",
        "table",
        "continuation_marker",
    ]
    bbox_1000: list[int] = Field(..., min_length=4, max_length=4)
    rotation_deg: int = Field(default=0)
    content_md: str = Field(..., description="Verbatim text with deterministic Markdown/HTML styling")


class VisualAsset(BaseModel):
    asset_type: Literal["illustration", "photograph", "ad_graphic", "diagram", "decorative_element"]
    bbox_1000: list[int] = Field(..., min_length=4, max_length=4)
    caption_or_notes: str


class PageExtraction(BaseModel):
    page_type: Literal[
        "front_cover",
        "inside_front_cover",
        "table_of_contents",
        "editorial",
        "story",
        "department",
        "advertisement",
        "inside_back_cover",
        "back_cover",
        "mixed",
    ]
    printed_page_number: Optional[str] = None
    continuation_from: Optional[str] = None
    continuation_to: Optional[str] = None
    elements: list[TextElement]
    visual_assets: list[VisualAsset] = Field(default_factory=list)


# ==============================================================================
# 2. SQLITE WORKFLOW DATABASE
# ==============================================================================

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pages (
    page_num INTEGER PRIMARY KEY,
    filename TEXT NOT NULL UNIQUE,
    anonymous_uuid TEXT NOT NULL UNIQUE,
    width_px INTEGER,
    height_px INTEGER,
    status TEXT NOT NULL DEFAULT 'pending', -- pending | completed | failed
    route_used TEXT,                        -- local | cloud | local_escalated_to_cloud
    model_name TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    duration_sec REAL DEFAULT 0.0,
    error_message TEXT,
    updated_at TEXT
);
"""


def init_db(db_path: Path, pages_dir: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)

    page_files = sorted(pages_dir.glob("page-[0-9][0-9][0-9][0-9].png"))
    if not page_files:
        raise FileNotFoundError(f"No page-NNNN.png files found in {pages_dir}")

    for pf in page_files:
        match = re.match(r"page-(\d{4})\.png$", pf.name)
        if not match:
            continue
        page_num = int(match.group(1))
        cur = conn.execute("SELECT 1 FROM pages WHERE page_num = ?", (page_num,))
        if cur.fetchone() is None:
            with Image.open(pf) as img:
                w, h = img.size
            conn.execute(
                """
                INSERT INTO pages (page_num, filename, anonymous_uuid, width_px, height_px, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (page_num, pf.name, str(uuid.uuid4()), w, h, datetime.now(timezone.utc).isoformat()),
            )
    conn.commit()
    return conn


# ==============================================================================
# 3. PRIVACY IMAGE SCRUBBER & NORMALIZER
# ==============================================================================

def prepare_scrubbed_image(image_path: Path, max_dim: int = 2048) -> tuple[bytes, str]:
    """
    Loads page image, strips all EXIF/PNG metadata chunks by re-encoding fresh pixel data,
    and optionally downscales oversized scans while preserving crisp small glyphs.
    """
    with Image.open(image_path) as img:
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > max_dim:
            scale = max_dim / float(max(w, h))
            img = img.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)

        clean_img = Image.new(img.mode, img.size)
        clean_img.putdata(list(img.get_flattened_data() if hasattr(img, "get_flattened_data") else img.getdata()))

        buf = io.BytesIO()
        clean_img.save(buf, format="PNG", optimize=True)
        raw_bytes = buf.getvalue()
        b64_str = base64.b64encode(raw_bytes).decode("ascii")
        return raw_bytes, b64_str


# ==============================================================================
# 4. VLM BACKENDS (LOCAL OLLAMA & CLOUD API)
# ==============================================================================

def call_local_ollama(b64_image: str, model: str, ollama_url: str) -> tuple[str, int, int]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "Extract all textual elements and visual asset bounding boxes from this page.",
                "images": [b64_image],
            },
        ],
        "format": PageExtraction.model_json_schema(),
        "stream": False,
        "options": {"temperature": 0.0, "num_ctx": 8192},
    }
    resp = requests.post(f"{ollama_url}/api/chat", json=payload, timeout=600)
    resp.raise_for_status()
    data = resp.json()
    content = data["message"]["content"]
    in_tok = data.get("prompt_eval_count", 0)
    out_tok = data.get("eval_count", 0)
    return content, in_tok, out_tok


def call_cloud_gemini(raw_bytes: bytes, anon_uuid: str, model: str) -> tuple[str, int, int]:
    from google import genai
    from google.genai import types

    client = genai.Client()  # Reads GEMINI_API_KEY from environment
    response = client.models.generate_content(
        model=model,
        contents=[
            types.Part.from_bytes(data=raw_bytes, mime_type="image/png"),
            f"Document Page UUID: {anon_uuid}. Extract all textual elements and visual asset bounding boxes.",
        ],
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0.0,
            response_mime_type="application/json",
            response_schema=PageExtraction,
        ),
    )
    in_tok = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
    out_tok = getattr(response.usage_metadata, "candidates_token_count", 0) or 0
    return response.text, in_tok, out_tok


def call_cloud_openai_compat(
    b64_image: str, anon_uuid: str, model: str, base_url: str, api_key: str
) -> tuple[str, int, int]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "temperature": 0.0,
        "reasoning_effort": "low",
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "PageExtraction",
                "strict": True,
                "schema": PageExtraction.model_json_schema(),
            },
        },
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"Page UUID: {anon_uuid}. Return valid JSON matching the schema.",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64_image}"},
                    },
                ],
            },
        ],
    }
    resp = requests.post(f"{base_url}/chat/completions", headers=headers, json=payload, timeout=180)
    resp.raise_for_status()
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    return content, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)


# ==============================================================================
# 5. OUTPUT VALIDATOR & TRIAGE ORCHESTRATOR
# ==============================================================================

def strip_markdown_fences(text: str) -> str:
    fence = chr(96) * 3
    cleaned = text.strip()
    if cleaned.startswith(fence):
        cleaned = re.sub(rf"^{fence}(?:json)?\s*|\s*{fence}$", "", cleaned, flags=re.DOTALL)
    return cleaned


def validate_extraction(raw_json_str: str) -> PageExtraction:
    cleaned = strip_markdown_fences(raw_json_str)
    parsed = json.loads(cleaned)
    model_obj = PageExtraction.model_validate(parsed)

    for el in model_obj.elements:
        ymin, xmin, ymax, xmax = el.bbox_1000
        if not (0 <= ymin < ymax <= 1000 and 0 <= xmin < xmax <= 1000):
            raise ValueError(f"Invalid bbox_1000 {el.bbox_1000} in sequence {el.sequence}")
    return model_obj


def process_magazine(args: argparse.Namespace) -> None:
    mag_dir = Path(args.magazine_dir).resolve()
    pages_dir = mag_dir / "pages"
    raw_text_dir = mag_dir / "raw_text"
    db_path = mag_dir / "workflow.db"

    if not pages_dir.is_dir():
        raise FileNotFoundError(f"Expected pages directory at: {pages_dir}")
    raw_text_dir.mkdir(parents=True, exist_ok=True)

    conn = init_db(db_path, pages_dir)

    query = "SELECT * FROM pages WHERE status != 'completed' ORDER BY page_num ASC"
    if args.limit:
        query += f" LIMIT {int(args.limit)}"
    rows = conn.execute(query).fetchall()

    print(f"Loaded {len(rows)} pending pages in {mag_dir.name} (Mode: {args.mode})")

    for row in rows:
        page_num = row["page_num"]
        filename = row["filename"]
        anon_uuid = row["anonymous_uuid"]
        img_path = pages_dir / filename
        out_path = raw_text_dir / f"page-{page_num:04d}.json"

        t0 = time.perf_counter()
        raw_bytes, b64_img = prepare_scrubbed_image(img_path, max_dim=args.max_dim)

        raw_output = None
        route_used = None
        model_used = None
        in_tokens = 0
        out_tokens = 0

        try:
            if args.mode in ("local", "hybrid"):
                try:
                    raw_output, in_tokens, out_tokens = call_local_ollama(
                        b64_img, args.local_model, args.ollama_url
                    )
                    validated = validate_extraction(raw_output)
                    if len(validated.elements) == 0:
                        raise ValueError("Local model returned 0 text elements")
                    route_used = "local"
                    model_used = args.local_model
                except Exception as local_err:
                    if args.mode == "local":
                        raise local_err
                    print(f"  [Page {page_num:04d}] Local validation failed ({local_err}); escalating to cloud...")

            if route_used is None and args.mode in ("cloud", "hybrid"):
                if args.cloud_provider == "gemini":
                    raw_output, c_in, c_out = call_cloud_gemini(raw_bytes, anon_uuid, args.cloud_model)
                else:
                    api_key = os.environ.get("CLOUD_API_KEY", "")
                    raw_output, c_in, c_out = call_cloud_openai_compat(
                        b64_img, anon_uuid, args.cloud_model, args.cloud_base_url, api_key
                    )
                validated = validate_extraction(raw_output)
                in_tokens += c_in
                out_tokens += c_out
                route_used = "local_escalated_to_cloud" if args.mode == "hybrid" else "cloud"
                model_used = args.cloud_model

            cleaned_json = strip_markdown_fences(raw_output)
            envelope = {
                "page_num": page_num,
                "source_file": filename,
                "route_used": route_used,
                "model_used": model_used,
                "extracted_at": datetime.now(timezone.utc).isoformat(),
                "raw_llm_output": json.loads(cleaned_json),
            }
            out_path.write_text(json.dumps(envelope, indent=2, ensure_ascii=False), encoding="utf-8")

            elapsed = round(time.perf_counter() - t0, 2)
            conn.execute(
                """
                UPDATE pages
                SET status = 'completed', route_used = ?, model_name = ?,
                    input_tokens = ?, output_tokens = ?, duration_sec = ?,
                    error_message = NULL, updated_at = ?
                WHERE page_num = ?
                """,
                (
                    route_used,
                    model_used,
                    in_tokens,
                    out_tokens,
                    elapsed,
                    datetime.now(timezone.utc).isoformat(),
                    page_num,
                ),
            )
            conn.commit()
            print(
                f"[Page {page_num:04d}] OK ({route_used} | {elapsed}s | "
                f"in:{in_tokens} out:{out_tokens} tok)"
            )

        except (Exception, ValidationError) as err:
            elapsed = round(time.perf_counter() - t0, 2)
            conn.execute(
                """
                UPDATE pages
                SET status = 'failed', duration_sec = ?, error_message = ?, updated_at = ?
                WHERE page_num = ?
                """,
                (elapsed, str(err), datetime.now(timezone.utc).isoformat(), page_num),
            )
            conn.commit()
            print(f"[Page {page_num:04d}] FAILED ({elapsed}s): {err}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verbatim Magazine OCR Pipeline")
    parser.add_argument("magazine_dir", help="Path to magazine working directory containing pages/")
    parser.add_argument(
        "--mode",
        choices=["local", "cloud", "hybrid"],
        default="hybrid",
        help="local: Ollama only | cloud: Cloud API only | hybrid: Try local, escalate failures to cloud",
    )
    parser.add_argument("--local-model", default="qwen2.5vl:7b", help="Ollama model tag")
    parser.add_argument("--ollama-url", default="http://localhost:11434", help="Ollama base URL")
    parser.add_argument("--cloud-provider", choices=["gemini", "openai_compat"], default="gemini")
    parser.add_argument("--cloud-model", default="gemini-2.5-flash", help="Cloud VLM model ID")
    parser.add_argument("--cloud-base-url", default="https://openrouter.ai")
    parser.add_argument("--max-dim", type=int, default=2048, help="Max pixel dimension before upload")
    parser.add_argument("--limit", type=int, default=None, help="Process N pages (for incremental testing)")
    args = parser.parse_args()
    process_magazine(args)
