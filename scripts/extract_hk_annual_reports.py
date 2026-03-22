# -*- coding: utf-8 -*-
"""
港股年报 PDF 章节提取器。

能力：
1) 自动检测目录页并解析章节与页码
2) 自动识别两栏排版并按左栏+右栏顺序提取
3) 按章节输出独立 Markdown
4) 每份年报额外输出一个“分章节合并 Markdown”
5) 支持批量处理目录下多个 PDF

依赖：pdfplumber, pyyaml
"""

from __future__ import annotations

import argparse
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

logging.getLogger("pdfminer").setLevel(logging.ERROR)


@dataclass
class Config:
    input_path: str = "./annual_reports"
    output_dir: str = "./annual_reports_markdown"
    min_chars: int = 300
    year: Optional[str] = None
    write_chapter_files: bool = True
    write_merged_file: bool = True
    merged_filename_template: str = "{year}_combined.md"


def load_config(config_path: Optional[str]) -> Config:
    if not config_path:
        return Config()

    cfg_file = Path(config_path)
    if not cfg_file.exists():
        raise SystemExit(f"配置文件不存在: {cfg_file}")

    with cfg_file.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    cfg = Config()
    for key in Config.__dataclass_fields__.keys():
        if key in raw:
            setattr(cfg, key, raw[key])
    return cfg


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="港股年报批量提取工具")
    parser.add_argument("--config", help="YAML 配置文件路径")
    parser.add_argument("-i", "--input", help="输入 PDF 文件或目录")
    parser.add_argument("-o", "--output", help="输出目录")
    parser.add_argument("--year", help="仅处理指定年份，例如 2024")
    parser.add_argument("--min-chars", type=int, help="章节最小字符数阈值")
    return parser.parse_args()


def merge_cli_overrides(cfg: Config, args: argparse.Namespace) -> Config:
    if args.input:
        cfg.input_path = args.input
    if args.output:
        cfg.output_dir = args.output
    if args.year:
        cfg.year = args.year
    if args.min_chars is not None:
        cfg.min_chars = args.min_chars
    return cfg


def get_year_from_filename(filename: str) -> Optional[str]:
    match = re.search(r"(\d{4})", filename)
    return match.group(1) if match else None


def safe_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name)


def extract_text_for_page(page: Any) -> str:
    """提取单页文本；若检测为两栏，按左栏+右栏拼接。"""
    width = page.width
    height = page.height
    mid = width / 2
    gutter = 6

    text_full = page.extract_text() or ""
    if not text_full:
        return ""

    words = page.extract_words() or []
    if not words:
        return text_full

    left_words: List[Tuple[float, float]] = []
    right_words: List[Tuple[float, float]] = []
    for w in words:
        try:
            x0 = float(w.get("x0", 0))
            x1 = float(w.get("x1", 0))
        except (TypeError, ValueError):
            continue
        center = (x0 + x1) / 2
        if center < mid - gutter:
            left_words.append((x0, x1))
        elif center > mid + gutter:
            right_words.append((x0, x1))

    if len(left_words) < 10 or len(right_words) < 10:
        return text_full

    left_max = max(x1 for _, x1 in left_words)
    right_min = min(x0 for x0, _ in right_words)
    gap = right_min - left_max
    if gap < 6:
        return text_full

    split = left_max + gap / 2
    left_bbox = (0, 0, split, height)
    right_bbox = (split, 0, width, height)
    left_text = page.crop(left_bbox).extract_text() or ""
    right_text = page.crop(right_bbox).extract_text() or ""

    def stats(text: str) -> Tuple[int, int]:
        chars = len(re.sub(r"\s+", "", text))
        lines = len([ln for ln in text.split("\n") if ln.strip()])
        return chars, lines

    full_chars, _ = stats(text_full)
    left_chars, left_lines = stats(left_text)
    right_chars, right_lines = stats(right_text)

    if left_lines < 4 or right_lines < 4:
        return text_full
    if full_chars > 0 and (left_chars + right_chars) < full_chars * 0.9:
        return text_full

    def dedupe_overlap(left: str, right: str, max_lines: int = 3) -> Tuple[str, str]:
        left_lines_arr = [ln for ln in left.split("\n") if ln.strip()]
        right_lines_arr = [ln for ln in right.split("\n") if ln.strip()]
        for i in range(1, max_lines + 1):
            if len(left_lines_arr) < i or len(right_lines_arr) < i:
                break
            left_tail = re.sub(r"\s+", "", "".join(left_lines_arr[-i:]))
            right_head = re.sub(r"\s+", "", "".join(right_lines_arr[:i]))
            if left_tail and (left_tail in right_head or right_head in left_tail):
                if left_tail in right_head:
                    del right_lines_arr[:i]
                else:
                    del left_lines_arr[-i:]
                break
        return "\n".join(left_lines_arr), "\n".join(right_lines_arr)

    left_clean, right_clean = dedupe_overlap(left_text, right_text)
    if left_clean and right_clean:
        merged = f"{left_clean}\n<<<COLUMN_BREAK>>>\n{right_clean}".strip()
    else:
        merged = f"{left_clean}{right_clean}".strip()

    return merged if merged else text_full


def extract_text_from_pdf(pdf_path: Path) -> Tuple[List[str], Dict[str, Any]]:
    pdfplumber = __import__("pdfplumber")

    pages: List[str] = []
    metadata: Dict[str, Any] = {}
    with pdfplumber.open(str(pdf_path)) as pdf:
        metadata = {"pages": len(pdf.pages), "metadata": pdf.metadata or {}}
        for page in pdf.pages:
            pages.append(extract_text_for_page(page))
    return pages, metadata


def build_page_number_map(
    pages: List[str], pdf_path: Optional[Path] = None
) -> Dict[int, int]:
    """构建 印刷页码 -> PDF页索引(1-based) 映射。"""

    def scan(page_texts: List[str]) -> Dict[int, int]:
        found: Dict[int, int] = {}
        for idx, page in enumerate(page_texts, start=1):
            if not page:
                continue
            lines = [line.strip() for line in page.split("\n") if line.strip()]
            if any(re.match(r"^目\s*[录錄]", line) for line in lines[:5]):
                continue
            candidates = lines[:3] + lines[-3:]
            page_num = None
            for line in candidates:
                m = re.match(r"^(\d{1,4})\b", line)
                if m:
                    candidate = int(m.group(1))
                    if re.match(r"^\d{1,4}$", line) or re.search(r"年报|年報", line):
                        page_num = candidate
                        break
                m = re.match(r"^第\s*(\d{1,4})\s*页", line)
                if m:
                    page_num = int(m.group(1))
                    break
                m = re.search(r"(\d{1,4})$", line)
                if m and re.search(r"股份有限公司|年报|年報", line):
                    page_num = int(m.group(1))
                    break
            if page_num is not None and 1 <= page_num <= 2000 and page_num not in found:
                found[page_num] = idx
        return found

    mapping = scan(pages)
    if mapping or not pdf_path:
        return mapping

    pdfplumber = __import__("pdfplumber")

    with pdfplumber.open(str(pdf_path)) as pdf:
        raw_pages = [(p.extract_text() or "") for p in pdf.pages]
    return scan(raw_pages)


def find_toc_pages(pages: List[str]) -> List[int]:
    toc_pages: List[int] = []
    scan_limit = max(3, int(len(pages) * 0.2))

    def count_toc_pairs(text: str) -> int:
        if not text:
            return 0
        pairs = re.findall(r"[\u4e00-\u9fffA-Za-z·（）()]{2,}\s*\d{1,3}", text)
        pairs += re.findall(r"\d{1,3}\s*[\u4e00-\u9fffA-Za-z·（）()]{2,}", text)
        return len(pairs)

    for i, page in enumerate(pages[:scan_limit]):
        if not page:
            continue
        lines = page.split("\n")
        normalized_lines = [
            re.sub(r"(?<=[\u4e00-\u9fffA-Za-z])\s+(?=[\u4e00-\u9fffA-Za-z])", "", ln)
            for ln in lines[:10]
        ]
        keyword_hit = any(
            re.search(r"目\s*[录錄]|TABLE\s*OF\s*CONTENTS|CONTENTS", ln, re.IGNORECASE)
            for ln in normalized_lines
        )
        toc_hits = 0
        for ln in lines:
            s = ln.strip()
            if re.search(r"第[一二三四五六七八九十\d]+[章节篇部].{0,40}\d{1,4}$", s):
                toc_hits += 1
            elif re.search(r"\.\.\.\.+\s*\d{1,4}$", s):
                toc_hits += 1
        if keyword_hit or toc_hits >= 3:
            toc_pages.append(i)

    expanded: List[int] = []
    for idx in toc_pages:
        expanded.append(idx)
        ni = idx + 1
        if ni < len(pages) and pages[ni]:
            lines = pages[ni].split("\n")
            next_hits = sum(
                1
                for ln in lines
                if re.search(
                    r"第[一二三四五六七八九十\d]+[章节篇部].{0,40}\d{1,4}$", ln.strip()
                )
                or re.search(r"\.\.\.\.+\s*\d{1,4}$", ln.strip())
            )
            if next_hits >= 3 or count_toc_pairs(pages[ni]) >= 4:
                expanded.append(ni)

    return sorted(set(expanded))


def parse_toc_from_page(page_text: str) -> List[Dict[str, Any]]:
    chapters: List[Dict[str, Any]] = []

    def normalize_spaced_text(value: str) -> str:
        return re.sub(
            r"(?<=[\u4e00-\u9fffA-Za-z])\s+(?=[\u4e00-\u9fffA-Za-z])", "", value
        )

    def is_noise_line(value: str) -> bool:
        if not value:
            return True
        if re.search(
            r"Designed|Wonderful Sky|Public Relations|Tel\.|Fax", value, re.IGNORECASE
        ):
            return True
        if re.match(r"^\d{1,4}$", value):
            return False
        if re.match(r"^[A-Za-z0-9 .():;/\-]+$", value):
            return True
        return False

    def add_entry(title: str, page_num: int) -> None:
        title = re.sub(r"\s+", " ", title).strip(" .·•")
        if title in {"目錄", "目录"}:
            return
        if is_noise_line(title):
            return
        if not re.search(r"[\u4e00-\u9fff]", title):
            return
        if not (1 <= page_num <= 800):
            return
        chapters.append({"number": "", "title": title, "page": page_num})

    lines = page_text.split("\n") if page_text else []
    pending_title: Optional[str] = None
    pending_page: Optional[int] = None
    left_nums: List[int] = []
    right_titles: List[str] = []
    in_right_column = False

    i = 0
    while i < len(lines):
        line = normalize_spaced_text(lines[i].strip())
        i += 1
        if not line:
            continue
        if line == "<<<COLUMN_BREAK>>>":
            in_right_column = True
            continue
        if re.match(r"^目\s*[录錄]\s*$", line) or re.match(
            r"^CONTENTS$", line, re.IGNORECASE
        ):
            continue
        if is_noise_line(line):
            continue

        chapter_match = re.match(
            r"^(第[一二三四五六七八九十\d]+[章节篇部])\s*[:：]?\s*(.+?)\s*(?:\.\.\.\.+\s*)?(\d+)\s*$",
            line,
        )
        if chapter_match:
            chapters.append(
                {
                    "number": chapter_match.group(1),
                    "title": chapter_match.group(2),
                    "page": int(chapter_match.group(3)),
                }
            )
            continue

        simple_match = re.match(r"^(.+?)\s*(?:\.\.\.\.+\s*)?(\d+)\s*$", line)
        if simple_match and len(simple_match.group(1)) > 2:
            title = simple_match.group(1).strip()
            if not re.match(r"^第\d+页$", title):
                add_entry(title, int(simple_match.group(2)))
            continue

        reverse_match = re.match(r"^(\d{1,4})\s+(.+?)\s*$", line)
        if reverse_match and len(reverse_match.group(2)) > 1:
            add_entry(reverse_match.group(2).strip(), int(reverse_match.group(1)))
            continue

        if re.match(r"^\d{1,4}$", line):
            if not in_right_column:
                left_nums.append(int(line))
                continue
            pending_page = int(line)
            if pending_title:
                add_entry(pending_title, pending_page)
                pending_title = None
                pending_page = None
            continue

        if len(line) > 2:
            if in_right_column and not re.search(r"\d", line):
                right_titles.append(line)
                continue
            next_line = lines[i].strip() if i < len(lines) else ""
            if re.match(r"^\d{1,4}$", next_line):
                add_entry(line, int(next_line))
                i += 1
                continue
            pending_title = line
            if pending_page:
                add_entry(pending_title, pending_page)
                pending_title = None
                pending_page = None

    if left_nums and right_titles:
        for title, page_num in zip(right_titles, left_nums):
            add_entry(title, page_num)

    return chapters


def resolve_chapter_pdf_pages(
    chapters: List[Dict[str, Any]], page_map: Dict[int, int], max_pages: int
) -> List[Dict[str, Any]]:
    known = sorted((k, v) for k, v in page_map.items())

    def fallback(print_page: int) -> int:
        if not known:
            return print_page
        for i in range(len(known) - 1):
            p1, pdf1 = known[i]
            p2, pdf2 = known[i + 1]
            if p1 <= print_page <= p2:
                return max(1, min(max_pages, pdf1 + (print_page - p1)))
        if print_page < known[0][0]:
            p1, pdf1 = known[0]
            return max(1, min(max_pages, pdf1 - (p1 - print_page)))
        p_last, pdf_last = known[-1]
        return max(1, min(max_pages, pdf_last + (print_page - p_last)))

    resolved: List[Dict[str, Any]] = []
    for ch in chapters:
        print_page = int(ch.get("page", 0))
        pdf_page = page_map.get(print_page, fallback(print_page))
        item = dict(ch)
        item["pdf_page"] = pdf_page
        resolved.append(item)
    return resolved


def normalize_chapters(
    chapters: List[Dict[str, Any]], max_pages: int
) -> List[Dict[str, Any]]:
    seen = set()
    cleaned: List[Dict[str, Any]] = []
    for ch in chapters:
        key = (ch.get("title", ""), int(ch.get("page", 0)))
        if key in seen:
            continue
        seen.add(key)
        pdf_page = int(ch.get("pdf_page", ch.get("page", 0)))
        print_page = int(ch.get("page", 0))
        if print_page > max_pages + 20:
            continue
        if 1 <= pdf_page <= max_pages:
            cleaned.append(ch)
    cleaned.sort(key=lambda x: int(x.get("pdf_page", x.get("page", 0))))
    return cleaned


def clean_text(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


def merge_wrapped_lines(text: str) -> str:
    if not text:
        return text

    if "<<<COLUMN_BREAK>>>" in text:
        parts = text.split("<<<COLUMN_BREAK>>>")
        cleaned = [merge_wrapped_lines(p.strip()) for p in parts if p.strip()]
        return "\n\n".join(cleaned).strip()

    lines = text.split("\n")
    merged: List[str] = []
    buffer = ""

    def is_header_line(value: str) -> bool:
        if re.match(r"^\d{1,3}$", value):
            return True
        if re.match(r"^\d{1,3}\s+", value) and re.search(r"年报|年報", value):
            return True
        if "股份有限公司" in value and re.search(r"年报|年報", value):
            return True
        return False

    def flush() -> None:
        nonlocal buffer
        if buffer:
            merged.append(buffer)
            buffer = ""

    for line in lines:
        s = line.strip()
        if not s:
            flush()
            merged.append("")
            continue
        if is_header_line(s):
            flush()
            continue
        if not buffer:
            buffer = s
            continue
        if buffer[-1] in {"。", "！", "？", ".", ";", ":"}:
            flush()
            buffer = s
        else:
            buffer = f"{buffer} {s}"

    flush()
    result = "\n".join(merged).strip()
    result = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", result)
    result = re.sub(r"(?<=[，。、；：])\s+(?=[\u4e00-\u9fff])", "", result)
    result = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[，。、；：])", "", result)
    return result


def trim_at_next_title(content: str, next_title: Optional[str]) -> str:
    if not next_title:
        return content
    lines = content.split("\n")
    for idx, line in enumerate(lines):
        s = line.strip()
        if not s:
            continue
        if s == next_title or (
            s.startswith(next_title) and len(s) <= len(next_title) + 6
        ):
            return "\n".join(lines[:idx]).rstrip()
        m = re.match(r"^\d{1,2}\s+(.+)$", s)
        if m and m.group(1).strip() == next_title:
            return "\n".join(lines[:idx]).rstrip()
    return content


def extract_section_content(pages: List[str], start_page: int, end_page: int) -> str:
    start_idx = max(start_page - 1, 0)
    end_idx = min(end_page - 1, len(pages) - 1)
    if start_idx > end_idx:
        return ""
    return "\n\n".join(pages[start_idx : end_idx + 1])


def default_chapters() -> List[Dict[str, Any]]:
    return [
        {"number": "1", "title": "公司基本情况", "page": 1},
        {"number": "2", "title": "会计数据摘要", "page": 10},
        {"number": "3", "title": "经营情况讨论", "page": 20},
        {"number": "4", "title": "重要事项", "page": 40},
        {"number": "5", "title": "财务报告", "page": 60},
    ]


def list_pdf_files(input_path: Path, year: Optional[str]) -> List[Path]:
    if input_path.is_file() and input_path.suffix.lower() == ".pdf":
        if year and year not in input_path.name:
            return []
        return [input_path]

    if not input_path.is_dir():
        raise SystemExit(f"输入路径不存在或不是目录/PDF文件: {input_path}")

    files = [
        p for p in input_path.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"
    ]
    if year:
        files = [p for p in files if year in p.name]
    return sorted(files)


def render_chapter_markdown(chapter: Dict[str, Any], content: str) -> str:
    chapter_number = str(chapter.get("number", "")).strip()
    title = str(chapter.get("title", "")).strip()
    heading = f"{chapter_number} {title}".strip()
    return f"# {heading}\n\n{content}" if heading else content


def write_chapter_files(
    output_dir: Path,
    year: str,
    chapters: List[Dict[str, Any]],
    chapter_contents: List[str],
) -> List[str]:
    generated: List[str] = []
    for idx, (chapter, content) in enumerate(zip(chapters, chapter_contents), start=1):
        title = safe_filename(str(chapter.get("title", "")))
        filename = f"{year}_{str(idx).zfill(2)}_{title}.md"
        file_path = output_dir / filename
        file_path.write_text(
            render_chapter_markdown(chapter, content), encoding="utf-8"
        )
        generated.append(filename)
    return generated


def write_merged_markdown(
    output_dir: Path,
    year: str,
    template: str,
    chapters: List[Dict[str, Any]],
    chapter_contents: List[str],
) -> str:
    merged_name = safe_filename(template.format(year=year))
    merged_path = output_dir / merged_name

    lines: List[str] = [f"# {year} 年报（分章节合并）", ""]
    lines.append("## 目录")
    lines.append("")
    for idx, chapter in enumerate(chapters, start=1):
        title = str(chapter.get("title", ""))
        lines.append(f"- {idx}. {title}")
    lines.append("")

    for idx, (chapter, content) in enumerate(zip(chapters, chapter_contents), start=1):
        title = str(chapter.get("title", ""))
        lines.append(f"## {idx}. {title}")
        lines.append("")
        lines.append(content)
        lines.append("")

    merged_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return merged_name


def validate_chapters(
    chapters: List[Dict[str, Any]],
    chapter_contents: List[str],
    max_pages: int,
    min_chars: int,
) -> List[str]:
    issues: List[str] = []
    for i, ch in enumerate(chapters):
        page_num = int(ch.get("page", 0))
        pdf_page = int(ch.get("pdf_page", page_num))
        if page_num < 1 or page_num > max_pages:
            issues.append(f"章节页码越界: {ch.get('title', '')} (第{page_num}页)")
        if pdf_page < 1 or pdf_page > max_pages:
            issues.append(f"章节PDF页码越界: {ch.get('title', '')} (PDF第{pdf_page}页)")
        if i + 1 < len(chapters):
            next_pdf = int(
                chapters[i + 1].get("pdf_page", chapters[i + 1].get("page", 0))
            )
            if next_pdf <= pdf_page:
                issues.append(
                    f"章节页码重叠/乱序: {ch.get('title', '')} -> {chapters[i + 1].get('title', '')}"
                )

    for i, content in enumerate(chapter_contents, start=1):
        char_count = len(re.sub(r"\s+", "", content))
        if char_count < min_chars:
            issues.append(f"章节内容过短: 第{i}章 (字符数 {char_count})")
    return issues


def process_pdf(pdf_path: Path, output_dir: Path, cfg: Config) -> Dict[str, Any]:
    year = get_year_from_filename(pdf_path.name)
    if not year:
        return {"year": "未知年份", "error": f"无法从文件名识别年份: {pdf_path.name}"}

    pages, metadata = extract_text_from_pdf(pdf_path)
    max_pages = len(pages)

    toc_pages = find_toc_pages(pages)
    chapters: List[Dict[str, Any]] = []
    for toc_page in toc_pages:
        chapters.extend(parse_toc_from_page(pages[toc_page]))

    used_default = False
    if not chapters:
        chapters = default_chapters()
        used_default = True

    page_map = build_page_number_map(pages, pdf_path)
    chapters = resolve_chapter_pdf_pages(chapters, page_map, max_pages)
    chapters = normalize_chapters(chapters, max_pages)
    if not chapters:
        chapters = default_chapters()
        used_default = True

    chapter_contents: List[str] = []
    for idx, chapter in enumerate(chapters, start=1):
        start_page = int(chapter.get("pdf_page", chapter.get("page", 1)))
        if idx < len(chapters):
            end_page = (
                int(chapters[idx].get("pdf_page", chapters[idx].get("page", max_pages)))
                - 1
            )
        else:
            end_page = max_pages

        content = extract_section_content(pages, start_page, end_page)
        next_title = chapters[idx].get("title") if idx < len(chapters) else None
        content = trim_at_next_title(content, next_title)
        content = clean_text(content)
        content = merge_wrapped_lines(content)
        chapter_contents.append(content)

    generated_files: List[str] = []
    merged_file: Optional[str] = None
    if cfg.write_chapter_files:
        generated_files = write_chapter_files(
            output_dir, year, chapters, chapter_contents
        )
    if cfg.write_merged_file:
        merged_file = write_merged_markdown(
            output_dir, year, cfg.merged_filename_template, chapters, chapter_contents
        )

    issues = validate_chapters(chapters, chapter_contents, max_pages, cfg.min_chars)

    return {
        "year": year,
        "pdf": pdf_path.name,
        "metadata": metadata,
        "toc_pages": toc_pages,
        "chapters": chapters,
        "used_default": used_default,
        "generated_files": generated_files,
        "merged_file": merged_file,
        "issues": issues,
    }


def write_summary(output_dir: Path, results: List[Dict[str, Any]]) -> None:
    lines: List[str] = ["# 港股年报提取汇总", ""]
    for result in results:
        year = result.get("year", "未知年份")
        lines.append(f"## {year}")
        lines.append(f"- 源文件: {result.get('pdf', '')}")
        if "error" in result:
            lines.append(f"- 错误: {result['error']}")
            lines.append("")
            continue
        lines.append(f"- 章节数: {len(result.get('chapters', []))}")
        lines.append(
            f"- 目录识别: {'默认结构' if result.get('used_default') else '成功'}"
        )
        merged_file = result.get("merged_file")
        lines.append(f"- 合并文件: {merged_file if merged_file else '未生成'}")
        if result.get("issues"):
            lines.append("- 问题:")
            for issue in result["issues"]:
                lines.append(f"  - {issue}")
        else:
            lines.append("- 状态: 正常")
        lines.append("")

    (output_dir / "00_summary.md").write_text(
        "\n".join(lines).strip() + "\n", encoding="utf-8"
    )


def main() -> None:
    args = parse_args()
    cfg = merge_cli_overrides(load_config(args.config), args)

    input_path = Path(cfg.input_path)
    output_dir = Path(cfg.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    pdf_files = list_pdf_files(input_path, cfg.year)
    if not pdf_files:
        raise SystemExit("未找到符合条件的 PDF 文件")

    results: List[Dict[str, Any]] = []
    for pdf in pdf_files:
        print(f"[INFO] 处理中: {pdf.name}")
        result = process_pdf(pdf, output_dir, cfg)
        if "error" in result:
            print(f"[ERROR] {result['error']}")
        else:
            print(
                f"[OK] {pdf.name} -> 章节 {len(result['chapters'])}，"
                f"合并文件 {result.get('merged_file', '未生成')}"
            )
        results.append(result)

    write_summary(output_dir, results)

    print(f"\n处理完成，共 {len(results)} 份年报")
    print(f"输出目录: {output_dir}")


if __name__ == "__main__":
    main()
