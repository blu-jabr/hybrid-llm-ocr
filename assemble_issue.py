#!/usr/bin/env python3
"""
Analog Magazine OCR Normalizer & Issue Assembler (v3)
"""

import argparse
import json
import os
import re
import shutil
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    Image = None

MONTH_MAP = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
    "mid-december": "13", "mid december": "13",
}

SECTION_DIR_MAP = {
    "serial": "serial",
    "novelettes": "novelettes",
    "novelette": "novelettes",
    "science fact": "science_Fact",
    "short stories": "short_Stories",
    "short story": "short_Stories",
    "state of the art": "state_of_the_Art",
    "reader's departments": "readers_Departments",
    "readers' departments": "readers_Departments",
    "readers departments": "readers_Departments",
}

DICT_WORDS = set()
for _dp in ("/usr/share/dict/words", "/usr/share/dict/american-english"):
    if os.path.exists(_dp):
        with open(_dp, encoding="utf-8", errors="ignore") as _f:
            DICT_WORDS = {w.strip().lower() for w in _f if w.strip().isalpha() and len(w.strip()) >= 2}
        break

CLOSED_WORDS = {
    "notwithstanding", "nowhere", "today", "tomorrow", "tonight", "sometimes",
    "somewhat", "somewhere", "everywhere", "anywhere", "meanwhile", "moreover",
    "furthermore", "nevertheless", "nonetheless", "however", "therefore",
    "heretofore", "thereafter", "underneath", "throughout", "without", "within",
    "outside", "inside", "upon", "into", "onto", "cannot", "nobody", "nothing",
    "anyone", "everyone", "someone", "noone", "anything", "everything", "something",
    "anybody", "everybody", "somebody", "themselves", "ourselves", "yourselves",
    "himself", "herself", "itself", "myself", "yourself", "alongside", "widespread",
    "overlook", "overlooked", "overlooking", "undertake", "understood", "understand",
    "background", "underground", "foreground", "afternoon", "overhead", "overcome",
}

TRUE_PREFIXES = {
    "self", "non", "anti", "pre", "post", "sub", "semi", "multi",
    "co", "re", "un", "mid", "well", "ill", "all", "high", "low",
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "half", "full", "open", "red", "blue", "black", "white", "dark", "light",
    "twentieth", "nineteenth", "twenty", "thirty", "forty", "fifty", "less", "than",
}


def slugify(text: str) -> str:
    clean = re.sub(r"[*`_#<>]", "", text)
    clean = re.sub(r"[^a-zA-Z0-9]+", "-", clean.strip().lower()).strip("-")
    return clean or "untitled"


def gfm_heading_id(heading_text: str) -> str:
    """Computes exact Pandoc/GFM heading anchor ID used by Apostrophe & MDHero."""
    clean = re.sub(r"[*`_#<>]", "", heading_text).strip().lower()
    clean = re.sub(r"[^\w\s-]", "", clean)
    clean = re.sub(r"\s+", "-", clean)
    return clean


def _should_keep_hyphen(left: str, right: str) -> bool:
    if right.isupper():
        return True
    ll, rl = left.lower(), right.lower()
    joined = ll + rl
    if joined in CLOSED_WORDS:
        return False
    if DICT_WORDS:
        # 1) If the joined word is a valid single English word (rangers, recent, example, suburban, highways, animals), always join!
        if joined in DICT_WORDS:
            return False
        # 2) Otherwise keep hyphen when both halves are standalone English words (moderate-sized, fast-food)
        if len(ll) >= 3 and len(rl) >= 3 and ll in DICT_WORDS and rl in DICT_WORDS:
            return True
    return ll in TRUE_PREFIXES


def repair_hyphens(text: str) -> str:
    def _join_strict(match):
        left, right = match.group(1), match.group(2)
        return f"{left}-{right}" if _should_keep_hyphen(left, right) else f"{left}{right}"

    def _join_wrap(match):
        left, right = match.group(1), match.group(2)
        if right.isupper():
            return f"{left}-{right}"
        # For explicit line-wrap / hyphen+space breaks (e.g. "fa- miliar", "Ten- nessee"),
        # join unless both halves are clear standalone words and joined isn't a single dictionary word
        if DICT_WORDS and (left.lower() + right.lower()) in DICT_WORDS:
            return f"{left}{right}"
        if DICT_WORDS and len(left) >= 3 and len(right) >= 3 and left.lower() in DICT_WORDS and right.lower() in DICT_WORDS:
            return f"{left}-{right}"
        return f"{left}{right}"

    text = text.replace("\\n", "\n")
    text = re.sub(r"([A-Za-z]{2,})-\s*\n\s*([a-z]{2,})", _join_wrap, text)
    text = re.sub(r"([A-Za-z]{2,})-\s+([a-z]{2,})", _join_wrap, text)
    text = re.sub(r"\b([A-Za-z]{2,})-([a-z]{2,})\b", _join_strict, text)
    return text


def resplit_collapsed_prose(para: str) -> list[str]:
    """Splits long single-line VLM blocks where paragraph breaks collapsed into spaces."""
    if len(para) < 380:
        return [para]
    # Split on speaker changes: ” “ or " "
    parts = re.split(r'(?<=[.!?…—][”"])\s+(?=[“"])', para)
    expanded = list()
    for p in parts:
        # Split on narration -> dialogue: sentence ending with . ! ? followed by “Quote
        sub1 = re.split(r'(?<=[.!?])\s+(?=[“"][A-Z])', p)
        for s1 in sub1:
            # Split on dialogue -> narration: ” Capitalized word (excluding dialogue tags like said/asked/replied)
            sub2 = re.split(
                r'(?<=[.!?][”"])\s+(?=(?!Said\b|Asked\b|Replied\b|Snapped\b|Muttered\b|Whispered\b|Shouted\b)[A-Z][a-z]{2,}\b)',
                s1,
            )
            expanded.extend(sub2)
    return [x.strip() for x in expanded if x.strip()]


def split_paragraphs(text: str, role: str) -> list[str]:
    text = text.strip()
    if not text:
        return list()
    text = repair_hyphens(text)

    if re.fullmatch(r"[\*\.\•\s]{3,}", text):
        return ["* * *"]

    if role in {"table", "masthead", "running_header", "running_footer", "page_number", "byline", "title", "subtitle", "graphic_text"}:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return ["  \n".join(lines)] if len(lines) > 1 else lines

    if "\n\n" in text:
        raw_blocks = re.split(r"\n\s*\n+", text)
        paras = list()
        for b in raw_blocks:
            joined = re.sub(r"\s*\n\s*", " ", b.strip()).lstrip()
            if joined:
                paras.extend(resplit_collapsed_prose(joined))
        return paras

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return list()
    if len(lines) == 1:
        return resplit_collapsed_prose(lines.pop(0).lstrip())

    paras = list()
    buf = lines.pop(0).lstrip()
    for nxt in lines:
        nxt = nxt.lstrip()
        if re.fullmatch(r"[\*\.\•\s]{3,}", nxt):
            if buf:
                paras.extend(resplit_collapsed_prose(buf))
            paras.append("* * *")
            buf = ""
            continue
        if not buf:
            buf = nxt
            continue
        if re.search(r'[.!?]["”’\')\]]?$', buf) and re.match(r'^["“‘\'(*#A-Z0-9]', nxt):
            paras.extend(resplit_collapsed_prose(buf))
            buf = nxt
        else:
            buf = f"{buf} {nxt}"
    if buf:
        paras.extend(resplit_collapsed_prose(buf))
    return paras


def bbox_overlap_ratio(inner: list[int], outer: list[int]) -> float:
    if len(inner) != 4 or len(outer) != 4:
        return 0.0
    iy1, ix1, iy2, ix2 = inner
    oy1, ox1, oy2, ox2 = outer
    inter_y = max(0, min(iy2, oy2) - max(iy1, oy1))
    inter_x = max(0, min(ix2, ox2) - max(ix1, ox1))
    inter_area = inter_y * inter_x
    inner_area = max(1, (iy2 - iy1) * (ix2 - ix1))
    return inter_area / inner_area


def normalize_page(raw_path: Path) -> dict:
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    out = raw.get("raw_llm_output", {})
    raw_els = out.get("elements") or list()

    norm_els = list()
    for idx, el in enumerate(raw_els, start=1):
        seq = el.get("sequence")
        if not isinstance(seq, int) or seq <= 0:
            seq = idx
        role = el.get("role") or "body"
        content = el.get("content_md") or el.get("text") or el.get("content") or el.get("markdown") or ""
        raw_bbox = el.get("bbox_1000") or list()
        bbox = [max(0, min(1000, int(v))) for v in raw_bbox]
        rot = int(el.get("rotation_deg") or 0)
        paras = split_paragraphs(content, role)
        norm_els.append({
            "sequence": seq,
            "role": role,
            "bbox_1000": bbox,
            "rotation_deg": rot,
            "content_md": "\n\n".join(paras) if paras else content.strip(),
            "paragraphs": paras,
        })

    norm_els.sort(key=lambda x: x["sequence"])
    for idx, el in enumerate(norm_els, start=1):
        el["sequence"] = idx

    raw_assets = out.get("visual_assets") or list()
    norm_assets = list()
    for idx, asset in enumerate(raw_assets, start=1):
        raw_bbox = asset.get("bbox_1000") or list()
        bbox = [max(0, min(1000, int(v))) for v in raw_bbox]
        desc = asset.get("description") or asset.get("caption_or_notes") or asset.get("caption") or ""
        atype = asset.get("type") or "illustration"
        norm_assets.append({
            "asset_index": idx,
            "type": atype,
            "bbox_1000": bbox,
            "description": desc.strip(),
            "cropped_image": None,
        })

    printed_raw = out.get("printed_page_number")
    printed_int = None
    if printed_raw is not None:
        m = re.search(r"\d+", str(printed_raw))
        if m:
            printed_int = int(m.group(0))

    return {
        "page_num": int(raw["page_num"]),
        "source_file": raw.get("source_file", f"page-{int(raw['page_num']):04d}.png"),
        "route_used": raw.get("route_used", "cloud"),
        "model_used": raw.get("model_used", ""),
        "extracted_at": raw.get("extracted_at", ""),
        "page_type": out.get("page_type") or "unknown",
        "printed_page_number": printed_int,
        "continuation_from": out.get("continuation_from"),
        "continuation_to": out.get("continuation_to"),
        "elements": norm_els,
        "visual_assets": norm_assets,
    }


def detect_issue_slug(pages: list[dict]) -> tuple[str, str]:
    for p in pages:
        if p["page_type"] == "table_of_contents":
            full = "\n".join(el["content_md"] for el in p["elements"])
            m = re.search(
                r"(January|February|March|April|May|June|July|August|September|October|November|December|Mid-December)[,\s]+(19\d{2}|20\d{2})",
                full,
                re.IGNORECASE,
            )
            if m:
                month_name = m.group(1)
                year = m.group(2)
                mm = MONTH_MAP[month_name.lower()]
                return f"Analog.{year}-{mm}", f"Analog Science Fiction/Science Fact — {month_name.title()} {year}"
    return "Analog.Issue", "Analog Science Fiction/Science Fact"


def parse_toc(toc_page: dict) -> tuple[list[dict], list[str], list[str]]:
    entries = list()
    header_notes = list()
    footer_notes = list()
    current_section = None
    seen_first_section = False

    for el in toc_page["elements"]:
        role = el["role"]
        txt = el["content_md"].strip()
        if not txt:
            continue

        if role == "subtitle":
            sec_clean = re.sub(r"[*#_]", "", txt).strip()
            sec_key = sec_clean.lower()
            if sec_key in SECTION_DIR_MAP:
                current_section = sec_clean
                seen_first_section = True
                continue

        if role == "table" and current_section:
            m = re.match(r"^(.*?)\s+(\d{1,3})$", txt)
            if m:
                left = m.group(1).strip()
                start_page = int(m.group(2))
                parts = [x.strip() for x in left.split(",")]
                title = parts.pop(0) if parts else "Untitled"
                author = ", ".join(parts)
                entries.append({
                    "section": current_section,
                    "section_dir": SECTION_DIR_MAP[current_section.lower()],
                    "title": title,
                    "author": author,
                    "start_page": start_page,
                })
                continue

        if not seen_first_section:
            header_notes.append(txt)
        else:
            footer_notes.append(txt)

    return entries, header_notes, footer_notes


def extract_masthead_box(pages: list[dict]) -> list[str]:
    box_items = list()
    for p in pages:
        if p["page_type"] in {"front_cover", "table_of_contents"}:
            continue
        mast_els = [el for el in p["elements"] if el["role"] == "masthead"]
        if len(mast_els) >= 2:
            for el in mast_els:
                box_items.append(el["content_md"].strip())
            break
    return box_items


def is_ornament_or_divider(asset: dict) -> bool:
    desc = (asset.get("description") or "").lower()
    atype = (asset.get("type") or "").lower()
    bbox = asset.get("bbox_1000") or list()
    if any(k in desc or k in atype for k in ("border", "divider", "ornament", "three dots", "three large dots", "separator", "line separating", "bullet point", "end-of-article", "horizontal rule")):
        return True
    if len(bbox) == 4:
        h = bbox[2] - bbox[0]
        w = bbox[3] - bbox[1]
        if h < 55 or w < 55:
            return True
    return False


def crop_assets(pages: list[dict], work_dir: Path, illus_dir: Path) -> None:
    if Image is None:
        return
    pages_dir = work_dir / "pages"
    for p in pages:
        if p["page_type"] in {"front_cover", "advertisement"}:
            continue
        img_path = pages_dir / p["source_file"]
        if not img_path.exists():
            continue
        ad_boxes = [el["bbox_1000"] for el in p["elements"] if el["role"] == "advertisement" and len(el["bbox_1000"]) == 4]
        valid_assets = list()
        for a in p["visual_assets"]:
            if is_ornament_or_divider(a):
                continue
            if any(bbox_overlap_ratio(a["bbox_1000"], ab) > 0.35 for ab in ad_boxes):
                a["is_ad_asset"] = True
                continue
            valid_assets.append(a)
        if not valid_assets:
            continue
        with Image.open(img_path) as im:
            w, h = im.size
            for asset in valid_assets:
                ymin, xmin, ymax, xmax = asset["bbox_1000"]
                left = int(xmin / 1000 * w)
                top = int(ymin / 1000 * h)
                right = int(xmax / 1000 * w)
                bottom = int(ymax / 1000 * h)
                if right - left < 40 or bottom - top < 40:
                    continue
                crop = im.crop((left, top, right, bottom))
                fname = f"p{p['page_num']:04d}-fig{asset['asset_index']}.png"
                crop.save(illus_dir / fname)
                asset["cropped_image"] = f"illustrations/{fname}"

    # Stitch two-page facing spreads (even page 2N + odd page 2N+1 where both are illustration-only pages)
    page_by_num = {p["page_num"]: p for p in pages}
    for pno in sorted(page_by_num):
        if pno % 2 != 0 or (pno + 1) not in page_by_num:
            continue
        left_p = page_by_num[pno]
        right_p = page_by_num[pno + 1]
        left_has_body = any(el["role"] == "body" for el in left_p["elements"])
        right_has_body = any(el["role"] == "body" for el in right_p["elements"])
        if left_has_body or right_has_body:
            continue
        left_imgs = [a for a in left_p["visual_assets"] if a.get("cropped_image")]
        right_imgs = [a for a in right_p["visual_assets"] if a.get("cropped_image")]
        if len(left_imgs) == 1 and len(right_imgs) == 1:
            la = left_imgs.pop(0)
            ra = right_imgs.pop(0)
            l_src = pages_dir / left_p["source_file"]
            r_src = pages_dir / right_p["source_file"]
            lpath = illus_dir.parent / la["cropped_image"]
            rpath = illus_dir.parent / ra["cropped_image"]
            if l_src.exists() and r_src.exists():
                with Image.open(l_src) as lim, Image.open(r_src) as rim:
                    target_h = max(lim.height, rim.height)
                    if lim.height != target_h:
                        lim = lim.resize((int(lim.width * target_h / lim.height), target_h))
                    if rim.height != target_h:
                        rim = rim.resize((int(rim.width * target_h / rim.height), target_h))
                    spread = Image.new("RGB", (lim.width + rim.width, target_h))
                    spread.paste(lim.convert("RGB"), (0, 0))
                    spread.paste(rim.convert("RGB"), (lim.width, 0))
                    spread_name = f"p{pno:04d}-{pno + 1:04d}-spread.png"
                    spread.save(illus_dir / spread_name)
                    lpath.unlink(missing_ok=True)
                    rpath.unlink(missing_ok=True)
                    la["cropped_image"] = f"illustrations/{spread_name}"
                    la["description"] = f"{la['description']} / {ra['description']}".strip(" /")
                    ra["cropped_image"] = None


def stitch_and_render_elements(elements: list[dict], assets: list[dict], img_prefix: str) -> str:
    skip_roles = {"running_header", "running_footer", "page_number", "masthead", "continuation_marker", "advertisement"}
    out_blocks = list()
    out_roles = list()
    pending_images = list()

    queue = list()
    assets_by_page = {}
    for a in assets:
        if a.get("cropped_image") and not a.get("is_ad_asset"):
            assets_by_page.setdefault(a.get("_page_num", 0), list()).append(a)

    seen_pages = set()
    for el in elements:
        pno = el.get("_page_num", 0)
        if pno not in seen_pages:
            seen_pages.add(pno)
            for a in assets_by_page.get(pno, list()):
                queue.append(("img", a))
        if el["role"] in skip_roles:
            continue
        queue.append(("el", el))

    for pno, alist in sorted(assets_by_page.items()):
        if pno not in seen_pages:
            for a in alist:
                queue.append(("img", a))

    for kind, obj in queue:
        if kind == "img":
            desc = obj["description"] or f"Illustration on page {obj.get('_page_num', '')}"
            pending_images.append(f"![{desc}]({img_prefix}{obj['cropped_image']})\n\n*{desc}*")
            continue

        el = obj
        role = el["role"]
        txt = el["content_md"].strip()
        if not txt:
            continue

        if role == "graphic_text":
            if re.match(r"^©\s*\d{4}", txt):
                continue
            out_blocks.append(f"*Illustration by {txt}*")
            out_roles.append("graphic_text")
            continue

        # Merge across blocks ONLY when both previous and current blocks are body prose
        if (
            role == "body"
            and out_blocks
            and out_roles[-1] == "body"
            and not out_blocks[-1].startswith(("#", ">", "|", "![", "* * *", "---"))
            and not re.search(r'[.!?]["”’\')\]]?$', out_blocks[-1])
            and not txt.startswith(("#", "* * *", "---"))
        ):
            paras = txt.split("\n\n")
            first_p = paras.pop(0).lstrip()
            out_blocks[-1] = repair_hyphens(f"{out_blocks[-1]} {first_p}")
            if pending_images:
                for img_md in pending_images:
                    out_blocks.append(img_md)
                    out_roles.append("image")
                pending_images.clear()
            for rem in paras:
                out_blocks.append(rem.lstrip())
                out_roles.append("body")
            continue

        if pending_images:
            for img_md in pending_images:
                out_blocks.append(img_md)
                out_roles.append("image")
            pending_images.clear()

        if role == "body":
            cleaned = "\n\n".join(p.lstrip() for p in txt.split("\n\n") if p.strip())
            out_blocks.append(cleaned)
            out_roles.append("body")
        else:
            out_blocks.append(txt)
            out_roles.append(role)

    if pending_images:
        for img_md in pending_images:
            out_blocks.append(img_md)
            out_roles.append("image")

    return "\n\n".join(out_blocks)


def get_ymin(el: dict) -> int:
    bbox = el.get("bbox_1000") or list()
    return bbox[0] if len(bbox) == 4 else 0


def split_page_at_title(page: dict, target_title: str | None = None) -> tuple[list[dict], list[dict], str | None]:
    els = page["elements"]
    title_candidates = [el for el in els if el["role"] == "title" and get_ymin(el) > 300]
    if not title_candidates:
        return els, list(), None

    chosen = None
    if target_title:
        norm_target = re.sub(r"[^a-z0-9]+", "", target_title.lower())
        for tc in title_candidates:
            norm_tc = re.sub(r"[^a-z0-9]+", "", tc["content_md"].lower())
            if norm_target and norm_target in norm_tc:
                chosen = tc
                break
    if chosen is None:
        chosen = title_candidates.pop(0)

    split_y = get_ymin(chosen)
    for a in page.get("visual_assets", list()):
        ab = a.get("bbox_1000") or list()
        if len(ab) == 4 and is_ornament_or_divider(a) and ab[0] < get_ymin(chosen):
            split_y = min(split_y, ab[0])

    for el in els:
        if el["role"] == "body" and get_ymin(el) < split_y:
            eb = el.get("bbox_1000") or list()
            if len(eb) == 4 and eb[0] > 400 and eb[2] <= get_ymin(chosen) + 15:
                split_y = min(split_y, eb[0])

    top_els = [el for el in els if get_ymin(el) < split_y]
    bot_els = [el for el in els if get_ymin(el) >= split_y]
    bot_titles = [el for el in bot_els if el["role"] in {"title", "subtitle"}]
    bot_rest = [el for el in bot_els if el["role"] not in {"title", "subtitle"}]
    detected_title = re.sub(r"[*#_]", "", chosen["content_md"]).strip()
    return top_els, bot_titles + bot_rest, detected_title


def split_bottom_quote_box(page: dict) -> tuple[list[dict], list[dict]]:
    """Separates filler quote boxes placed beneath a horizontal divider rule on editorial/story pages."""
    divider_y = None
    for a in page.get("visual_assets", list()):
        desc = (a.get("description") or "").lower()
        ab = a.get("bbox_1000") or list()
        if len(ab) == 4 and ("separating" in desc or ("horizontal rule" in desc and ab[0] > 200)):
            divider_y = ab[0]
            break
    if divider_y is None:
        return page["elements"], list()
    top_els = [el for el in page["elements"] if get_ymin(el) < divider_y]
    quote_els = [el for el in page["elements"] if get_ymin(el) >= divider_y and el["role"] in {"body", "byline"}]
    return top_els, quote_els


def format_quote_box(quote_els: list[dict]) -> str:
    if not quote_els:
        return ""
    lines = ["---"]
    for el in quote_els:
        txt = el["content_md"].strip()
        if not txt:
            continue
        if el["role"] == "byline":
            lines.append(f"— *{txt}*")
        else:
            lines.append(f"> {txt}")
    return "\n\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Normalize and assemble an Analog OCR issue")
    parser.add_argument("work_dir", help="Magazine working directory containing raw_text/ and pages/")
    parser.add_argument("--slug", default=None, help="Override issue slug (e.g. Analog.1985-01)")
    args = parser.parse_args()

    work_dir = Path(args.work_dir).resolve()
    raw_dir = work_dir / "raw_text"
    norm_dir = work_dir / "normalized"
    norm_dir.mkdir(exist_ok=True)

    raw_files = sorted(raw_dir.glob("page-*.json"))
    if not raw_files:
        raise SystemExit(f"No page-*.json files found in {raw_dir}")

    pages = [normalize_page(rf) for rf in raw_files]
    detected_slug, issue_title = detect_issue_slug(pages)
    slug = args.slug or detected_slug

    issue_dir = work_dir / slug
    if issue_dir.exists():
        shutil.rmtree(issue_dir)
    issue_dir.mkdir(parents=True)

    subdirs = [
        "advertisements", "illustrations", "novelettes", "readers_Departments",
        "science_Fact", "serial", "short_Stories", "state_of_the_Art",
    ]
    for sd in subdirs:
        (issue_dir / sd).mkdir(exist_ok=True)

    cover_src = work_dir / "pages" / "page-0001.png"
    cover_dst = issue_dir / "cover.png"
    if cover_src.exists():
        shutil.copy2(cover_src, cover_dst)

    crop_assets(pages, work_dir, issue_dir / "illustrations")

    for np in pages:
        for el in np["elements"]:
            el["_page_num"] = np["page_num"]
        for a in np["visual_assets"]:
            a["_page_num"] = np["page_num"]
        clean_np = {k: v for k, v in np.items()}
        clean_np["elements"] = [{k: v for k, v in el.items() if not k.startswith("_")} for el in np["elements"]]
        clean_np["visual_assets"] = [{k: v for k, v in a.items() if not k.startswith("_")} for a in np["visual_assets"]]
        (norm_dir / f"page-{np['page_num']:04d}.json").write_text(
            json.dumps(clean_np, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    toc_pages = [p for p in pages if p["page_type"] == "table_of_contents"]
    if not toc_pages:
        raise SystemExit("Could not find a page with page_type == 'table_of_contents'")
    toc_page = toc_pages.pop(0)

    entries, header_notes, footer_notes = parse_toc(toc_page)
    masthead_box = extract_masthead_box(pages)
    page_by_num = {p["page_num"]: p for p in pages}
    max_page = max(page_by_num)

    sorted_entries = sorted(entries, key=lambda e: e["start_page"])
    for idx, ent in enumerate(sorted_entries):
        if idx + 1 < len(sorted_entries):
            nxt_start = sorted_entries[idx + 1]["start_page"]
            nxt_pg = page_by_num.get(nxt_start)
            ent["end_page"] = nxt_start if (nxt_pg and nxt_pg["page_type"] == "mixed") else (nxt_start - 1)
        else:
            ent["end_page"] = max_page

    jump_targets = set()
    for p in pages:
        if p.get("continuation_to"):
            m = re.search(r"\d+", str(p["continuation_to"]))
            if m:
                jump_targets.add(int(m.group(0)))

    rendered_items = list()
    standalone_extras = list()

    for idx, ent in enumerate(sorted_entries):
        sec_dir = ent["section_dir"]
        fname = f"{ent['start_page']:03d}-{slugify(ent['title'])}.md"
        rel_link = f"{sec_dir}/{fname}"
        collected_els = list()
        collected_assets = list()
        trailing_quotes = list()
        next_ent_title = sorted_entries[idx + 1]["title"] if idx + 1 < len(sorted_entries) else None

        for pno in range(ent["start_page"], ent["end_page"] + 1):
            if pno in jump_targets:
                continue
            pg = page_by_num.get(pno)
            if not pg or pg["page_type"] == "advertisement":
                continue
            if pg["page_type"] == "mixed":
                top_els, bot_els, det_title = split_page_at_title(pg, next_ent_title if pno == ent["end_page"] else None)
                if pno == ent["start_page"] and idx > 0 and sorted_entries[idx - 1]["end_page"] == pno:
                    collected_els.extend(bot_els)
                elif pno == ent["end_page"] and bot_els:
                    collected_els.extend(top_els)
                    if not next_ent_title or re.sub(r"[^a-z0-9]+", "", next_ent_title.lower()) not in re.sub(r"[^a-z0-9]+", "", (det_title or "").lower()):
                        standalone_extras.append((pno, det_title or "Notice", bot_els))
                else:
                    collected_els.extend(pg["elements"])
            else:
                main_els, q_els = split_bottom_quote_box(pg)
                collected_els.extend(main_els)
                if q_els:
                    trailing_quotes.extend(q_els)
            collected_assets.extend(pg["visual_assets"])
            if pg.get("continuation_to"):
                m = re.search(r"\d+", str(pg["continuation_to"]))
                if m and int(m.group(0)) in page_by_num:
                    tpg = page_by_num[int(m.group(0))]
                    collected_els.extend(tpg["elements"])
                    collected_assets.extend(tpg["visual_assets"])

        quote_md = format_quote_box(trailing_quotes)
        body_multi = stitch_and_render_elements(collected_els, collected_assets, img_prefix="../")
        if quote_md:
            body_multi = f"{body_multi}\n\n{quote_md}"
        header_line = f"# {ent['title']}" + (f" — *{ent['author']}*" if ent["author"] else "")
        item_md = (
            f"[← Back to Contents](../contents.md)\n\n{header_line}\n\n"
            f"*Section: {ent['section']} | Pages {ent['start_page']}–{ent['end_page']}*\n\n---\n\n"
            f"{body_multi}\n\n---\n\n[← Back to Contents](../contents.md)\n"
        )
        (issue_dir / rel_link).write_text(item_md, encoding="utf-8")

        body_single = stitch_and_render_elements(collected_els, collected_assets, img_prefix=f"{slug}/")
        if quote_md:
            body_single = f"{body_single}\n\n{quote_md}"
        single_heading = f"## {ent['title']}" + (f" — *{ent['author']}*" if ent["author"] else "")
        rendered_items.append({
            "section": ent["section"], "title": ent["title"], "author": ent["author"],
            "page": ent["start_page"], "rel_link": rel_link, "single_heading": single_heading,
            "anchor": gfm_heading_id(single_heading), "body_single": body_single,
        })

    for pno, ex_title, ex_els in standalone_extras:
        ex_rel = f"readers_Departments/{pno:03d}-{slugify(ex_title)}.md"
        ex_body_multi = stitch_and_render_elements(ex_els, list(), img_prefix="../")
        (issue_dir / ex_rel).write_text(f"[← Back to Contents](../contents.md)\n\n{ex_body_multi}\n\n---\n\n[← Back to Contents](../contents.md)\n", encoding="utf-8")
        ex_body_single = stitch_and_render_elements(ex_els, list(), img_prefix=f"{slug}/")
        single_heading = f"## {ex_title}"
        rendered_items.append({
            "section": "Reader's Departments", "title": ex_title, "author": "",
            "page": pno, "rel_link": ex_rel, "single_heading": single_heading,
            "anchor": gfm_heading_id(single_heading), "body_single": ex_body_single,
        })

    ad_links = list()
    for p in pages:
        ad_els = [el for el in p["elements"] if el["content_md"].strip() and (p["page_type"] == "advertisement" or el["role"] == "advertisement")]
        if not ad_els:
            continue
        clean_label = re.sub(r"[*#_]", "", ad_els.pop(0)["content_md"]).splitlines().pop(0)[:60].strip()
        ad_rel = f"advertisements/ad-p{p['page_num']:03d}-{slugify(clean_label)}.md"
        ad_links.append((p["page_num"], clean_label, ad_rel))
        ad_body = "\n\n".join(el["content_md"] for el in p["elements"] if el["content_md"].strip() and (p["page_type"] == "advertisement" or el["role"] == "advertisement"))
        (issue_dir / ad_rel).write_text(f"[← Back to Contents](../contents.md)\n\n# Advertisement (Page {p['page_num']})\n\n{ad_body}\n\n[← Back to Contents](../contents.md)\n", encoding="utf-8")

    contents_lines = [f"# {issue_title}\n"] + ([f"![{issue_title} Cover](cover.png)\n"] if cover_dst.exists() else list()) + [
        " | ".join(h.replace("\n", " ") for h in header_notes) + "\n",
        "## Table of Contents\n", "| Section | Title | Author | Page |", "| :--- | :--- | :--- | ---: |",
    ] + [f"| {it['section']} | [{it['title']}]({it['rel_link']}) | {it['author']} | {it['page']} |" for it in sorted(rendered_items, key=lambda x: x["page"])]

    if masthead_box:
        contents_lines += ["\n## Masthead & Staff\n", "| Staff & Publishing Details |", "| :--- |"] + [f"| {ln.strip()} |" for blk in masthead_box for ln in blk.splitlines() if ln.strip()]
    if ad_links:
        contents_lines += ["\n## Advertisements\n", "| Page | Advertiser / Headline |", "| ---: | :--- |"] + [f"| {pno} | [{lbl}]({rel}) |" for pno, lbl, rel in ad_links]
    if footer_notes:
        contents_lines += ["\n---\n"] + [f"{fn}\n" for fn in footer_notes]
    (issue_dir / "contents.md").write_text("\n".join(contents_lines) + "\n", encoding="utf-8")

    single_lines = [f"# {issue_title}\n"] + ([f"![{issue_title} Cover]({slug}/cover.png)\n"] if cover_dst.exists() else list()) + [
        " | ".join(h.replace("\n", " ") for h in header_notes) + "\n",
        "## Table of Contents\n", "| Section | Title | Author | Page |", "| :--- | :--- | :--- | ---: |",
    ] + [f"| {it['section']} | [{it['title']}](#{it['anchor']}) | {it['author']} | {it['page']} |" for it in sorted(rendered_items, key=lambda x: x["page"])]

    if masthead_box:
        single_lines += ["\n## Masthead & Staff\n", "| Staff & Publishing Details |", "| :--- |"] + [f"| {ln.strip()} |" for blk in masthead_box for ln in blk.splitlines() if ln.strip()]
    single_lines.append("\n---\n")
    for it in sorted(rendered_items, key=lambda x: x["page"]):
        single_lines += [it["single_heading"] + "\n", it["body_single"] + "\n", "[↑ Back to Table of Contents](#table-of-contents)\n\n---\n"]

    single_md_path = work_dir / f"{slug}.md"
    single_md_path.write_text("\n".join(single_lines), encoding="utf-8")
    print(f"Normalized {len(pages)} pages -> {norm_dir}/\nBuilt directory tree -> {issue_dir}/\nBuilt single Markdown -> {single_md_path}")


if __name__ == "__main__":
    main()
