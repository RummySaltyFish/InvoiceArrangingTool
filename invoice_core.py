"""Offline PDF invoice extraction and name normalization."""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path


MAX_FILENAME_STEM_LENGTH = 100


@dataclass
class Invoice:
    path: Path
    number: str = ""
    name: str = ""
    total: Decimal | None = None
    code: str = ""
    error: str = ""
    engine: str = "文本"
    original_names: str = ""
    pdf_name: str = ""

    @property
    def key(self):
        return self.code, self.number


def compact(text):
    return re.sub(r"\s+", "", text)


def money(text):
    value = re.sub(r"[¥￥,，\s]", "", text).replace("．", ".").replace("−", "-")
    if not re.fullmatch(r"[+-]?\d+(?:\.\d{1,2})?", value):
        raise ValueError("金额必须是数字，最多两位小数")
    try:
        return Decimal(value).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValueError("金额无效") from exc


def remove_square_notes(value):
    """Remove square-bracket notes, including their contents."""
    return re.sub(r"【[^】]*】|\[[^\]]*\]", "", value)


def normalize_name(value):
    return re.sub(r"\s+", " ", value).strip(" 、,，;；。.")


def clean_name(value):
    value = value.replace("＊", "*").strip()
    # Joining PDF line wraps must not insert spaces into Chinese words.
    value = re.sub(r"\s*\n\s*", "", value)
    value = re.sub(r"^(?:\s*\*[^*]*\*)+", "", value).strip()
    value = remove_square_notes(value).replace("价外费用", "")
    value = re.sub(r"\s*[+＋]\s*", "和", value)
    value = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", value)
    value = re.sub(r"\s+", " ", value).strip(" 、,，;；")
    repeated = re.fullmatch(r"(.{2,}?)\1+", value)
    if repeated:
        value = repeated[1]
    # Only normalize explicit fastener terms; do not guess categories for other goods.
    terms = re.findall(r"螺栓|螺母|螺帽|螺丝钉?|螺钉|垫圈|垫片", value)
    if terms and not re.search(r"刀|机|胶|扳手|测试|检测|套筒|工具", value):
        terms = [{"螺帽": "螺母", "螺丝": "螺钉", "螺丝钉": "螺钉"}.get(t, t) for t in terms]
        value = "、".join(dict.fromkeys(terms))
    return value


def summarize_names(names):
    result = []
    for name in names:
        cleaned = clean_name(name)
        # Hardware summaries may already contain several canonical names.
        for item in cleaned.split("、"):
            if item and item not in result:
                result.append(item)
    return normalize_name("、".join(result))


def clean_spec(value):
    value = re.sub(r"\s*\n\s*", "", value).strip()
    value = remove_square_notes(value)
    if any(slash in value for slash in '/／⁄∕'):
        return ""
    # The dimension symbol is readable in both the workbook and Windows filenames.
    value = value.replace('*', '×').replace('＊', '×')
    value = re.sub(r"\s*[+＋]\s*", "和", value)
    return re.sub(r"\s+", " ", value).strip(" 、,，;；。.")


def contains_chinese(value):
    return bool(re.search(r"[\u4e00-\u9fff]", value))


def size_markers(specs):
    """Return at most the meaningful ASCII/dimension tokens from fastener specs."""
    result = []
    for spec in specs:
        compact_spec = re.sub(r"\s+", "", spec)
        tokens = re.findall(r"[A-Za-z]+\d+(?:\.\d+)?(?:×[A-Za-z0-9.]+)*|\d+(?:\.\d+)?(?:×[A-Za-z0-9.]+)+",
                            compact_spec)
        for token in tokens:
            if token not in result:
                result.append(token)
    return result


def fastener_item(name, specs):
    """Keep only size markings for screw/bolt specs and show at most two sizes."""
    label = name.replace("、", "")
    sizes = size_markers(specs)
    if not sizes:
        return label
    shown, omitted = sizes[:2], len(sizes) > 2
    suffix = "等" if omitted else ""
    return label + "、".join(shown) + suffix


def summarize_items(items):
    grouped = {}
    for raw_name, raw_spec in items:
        name, spec = clean_name(raw_name), clean_spec(raw_spec)
        if not name:
            continue
        if spec.startswith(name):
            spec = spec[len(name):].strip()
        specs = grouped.setdefault(name, [])
        if spec and spec not in specs:
            specs.append(spec)
    result = []
    electronic = {"单片机", "电子元件", "电子元器件"}
    for name, specs in grouped.items():
        if ("螺钉" in name or "螺栓" in name):
            item = fastener_item(name, specs)
        elif name in electronic and any(contains_chinese(spec) for spec in specs):
            item = "、".join(specs)
        else:
            item = name + ("、".join(specs) if specs else "")
        if item and item not in result:
            result.append(item)
    return normalize_name("、".join(result))


def group_rows(entries):
    rows = []
    for e in sorted(entries, key=lambda s: (s["y"], s["x0"])):
        row = next((r for r in reversed(rows[-3:]) if abs(r["y"] - e["y"]) < max(r["h"], e["h"]) * .6), None)
        if row is None:
            row = {"y": e["y"], "h": e["h"], "entries": []}
            rows.append(row)
        row["entries"].append(e)
    for row in rows:
        row["entries"].sort(key=lambda e: e["x0"])
        row["text"] = " ".join(e["text"] for e in row["entries"])
    return rows


def items_from_page(entries):
    rows = group_rows(entries)
    header = next((e for e in entries if any(t in compact(e["text"]) for t in ("项目名称", "货物或应税劳务", "货物或应税服务"))), None)
    if header is None:
        return []
    headings = [e for e in entries if abs(e["y"] - header["y"]) < header["h"] * 2
                and e["x0"] > header["x0"]]
    spec_header = next((e for e in headings if compact(e['text']) == '规格型号'), None)
    unit_header = next((e for e in headings if compact(e['text']) == '单位'), None)
    next_col = spec_header or unit_header
    if next_col is None:
        return []
    # OCR boxes can drift a few pixels left of their column heading.
    right = next_col['x0'] - header["h"] * .6
    spec_right = unit_header['x0'] - header['h'] * .6 if spec_header and unit_header else right
    end = min((r["y"] for r in rows if r["y"] > header["y"] and
               ("合计" in compact(r["text"]) or compact(r["text"]).startswith("备注"))), default=float("inf"))
    items, current_name, current_spec = [], [], []
    for row in rows:
        if not (header['y'] + header['h'] * .5 < row['y'] < end - header['h'] * .4):
            continue
        name = ''.join(e['text'].strip() for e in row['entries'] if e['x0'] < right).replace('＊', '*')
        spec = ''.join(e['text'].strip() for e in row['entries'] if right <= e['x0'] < spec_right)
        numeric_row = any(re.fullmatch(r'[¥￥+\-\d.,%\s]+', e['text'])
                          for e in row['entries'] if e['x0'] >= spec_right)
        starts_item = name.startswith('*') and ''.join(current_name).count('*') >= 2
        if current_name and name and (starts_item or numeric_row):
            items.append(('\n'.join(current_name), '\n'.join(current_spec)))
            current_name, current_spec = [], []
        if name:
            current_name.append(name)
        if spec and current_name:
            current_spec.append(spec)
    if current_name:
        items.append(('\n'.join(current_name), '\n'.join(current_spec)))
    return items


def parse_pages(path, pages, engine="文本"):
    text = "\n".join(r["text"] for page in pages for r in group_rows(page))
    flat = compact(text).replace("（", "(").replace("）", ")").replace("．", ".").replace("−", "-")
    numbers = list(dict.fromkeys(re.findall(r"发票(?:号码|号)[:：]?([0-9]{8,24})(?!\d)", flat)))
    invoice = Invoice(Path(path), engine=engine)
    if len(numbers) == 1:
        invoice.number = numbers[0]
    code = re.search(r"发票代码[:：]?(\d{10,12})(?!\d)", flat)
    if code:
        invoice.code = code[1]
    totals = []
    for page in pages:
        rows = group_rows(page)
        for row in rows:
            t = compact(row["text"]).replace("．", ".").replace("−", "-")
            if "价税合计" not in t:
                continue
            # Only the number immediately following (小写) on this row is authoritative.
            match = re.search(r"[（(]小写[）)][:：]?[¥￥]?([+-]?\d[\d,]*\.\d{2})(?!\d)", t)
            if match:
                totals.append(money(match[1]))
    totals = list(dict.fromkeys(totals))
    if len(totals) == 1:
        invoice.total = totals[0]
    items = [item for page in pages for item in items_from_page(page)]
    invoice.original_names = '\n\n'.join('项目：' + name + ('\n规格：' + spec if spec else '') for name, spec in items)
    invoice.name = summarize_items(items)
    if invoice.name:
        invoice.name = pdf_stem(invoice.name)
    errors = []
    if len(numbers) > 1:
        errors.append("一个文件含多张发票，请拆分后读取")
    elif not invoice.number:
        errors.append("未识别发票号码")
    if not invoice.name:
        errors.append("未识别品名")
    if invoice.total is None:
        errors.append("未识别价税合计行中（小写）后的唯一金额")
    invoice.error = "；".join(errors)
    return invoice


_ocr = None


def ocr_page(page):
    global _ocr
    import numpy as np
    from rapidocr import RapidOCR
    if _ocr is None:
        import rapidocr
        model_dir = Path(rapidocr.__file__).parent / "models"
        model_names = {"Det": "PP-OCRv6_det_small.onnx", "Cls": "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
                       "Rec": "PP-OCRv6_rec_small.onnx"}
        # Explicit local paths prevent model downloads at first use.
        params = {f"{k}.model_path": str(model_dir / v) for k, v in model_names.items()}
        missing = [v for v in model_names.values() if not (model_dir / v).is_file()]
        if missing:
            raise ValueError("本地扫描识别模型缺失")
        params["Global.log_level"] = "error"
        params["EngineConfig.onnxruntime.intra_op_num_threads"] = 4
        params["EngineConfig.onnxruntime.inter_op_num_threads"] = 2
        _ocr = RapidOCR(params=params)
    import pymupdf
    pix = page.get_pixmap(matrix=pymupdf.Matrix(2.5, 2.5), colorspace=pymupdf.csRGB, alpha=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3).copy()
    result = _ocr(arr)
    entries = []
    for text, box in zip(result.txts or (), result.boxes if result.boxes is not None else ()):
        xs, ys = [float(p[0]) for p in box], [float(p[1]) for p in box]
        entries.append({"text": text, "x0": min(xs), "x1": max(xs), "y": (min(ys) + max(ys)) / 2,
                        "h": max(ys) - min(ys)})
    return entries


def read_invoice(path):
    import pymupdf
    path = Path(path)
    try:
        with pymupdf.open(path) as document:
            if document.needs_pass:
                raise ValueError("PDF 有密码，请先解除密码")
            pages, used_ocr = [], False
            for page in document:
                entries = []
                for block in page.get_text("dict").get("blocks", []):
                    for line in block.get("lines", []):
                        for span in line.get("spans", []):
                            x0, y0, x1, y1 = span["bbox"]
                            if span["text"].strip():
                                entries.append({"text": span["text"], "x0": x0, "x1": x1,
                                                "y": (y0 + y1) / 2, "h": y1 - y0})
                if len(compact("".join(e["text"] for e in entries))) < 30:
                    entries = ocr_page(page)
                    used_ocr = True
                pages.append(entries)
            return parse_pages(path, pages, "扫描识别" if used_ocr else "文本")
    except Exception as exc:
        return Invoice(path, error=str(exc) or type(exc).__name__)


def scan_folder(folder, recursive=False, progress=None):
    folder = Path(folder)
    if not folder.is_dir():
        raise ValueError("请选择存在的发票文件夹")
    files = sorted((p for p in (folder.rglob("*") if recursive else folder.iterdir())
                    if p.is_file() and p.suffix.lower() == ".pdf"), key=lambda p: str(p.relative_to(folder)).casefold())
    if not files:
        raise ValueError("文件夹中没有 PDF 文件")
    invoices, seen, duplicates = [], {}, []
    for i, path in enumerate(files, 1):
        if progress:
            progress(i, len(files), path.name)
        invoice = read_invoice(path)
        if invoice.number and invoice.key in seen:
            previous = seen[invoice.key]
            if not invoice.error and not previous.error and invoice.total == previous.total and invoice.name == previous.name:
                if matching_filename(invoice) and not matching_filename(previous):
                    invoices[invoices.index(previous)] = invoice
                    seen[invoice.key] = invoice
                    duplicates = [(p, invoice.path if first == previous.path else first) for p, first in duplicates]
                    duplicates.append((previous.path, invoice.path))
                    continue
                duplicates.append((path, previous.path))
                continue
            invoice.error = "相同发票号码的内容不一致，请核对后移除重复项；" + invoice.error
        elif invoice.number:
            seen[invoice.key] = invoice
        invoices.append(invoice)
    prepare_names(invoices)
    return invoices, duplicates


def pdf_stem(name):
    """Normalize a product name so it can also serve as a Windows PDF stem."""
    substitutions = str.maketrans({'<': '＜', '>': '＞', ':': '：', '"': '＂', '/': '／',
                                  '\\': '＼', '|': '｜', '?': '？', '*': '×'})
    stem = remove_square_notes(name).replace('价外费用', '')
    stem = re.sub(r'\s*[+＋]\s*', '和', stem)
    stem = re.sub(r'\s+', ' ', stem).translate(substitutions)
    stem = re.sub(r'[\x00-\x1f]', '', stem).strip().rstrip('. ')
    stem = normalize_name(stem)[:MAX_FILENAME_STEM_LENGTH].rstrip('. ')
    if not stem:
        raise ValueError("品名不能用作文件名")
    if re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", stem, re.I):
        stem = '_' + stem
    return stem


def matching_filename(invoice):
    stem = pdf_stem(invoice.name)
    actual = invoice.path.stem
    if actual.casefold() == stem.casefold():
        return True
    match = re.search(r"（([2-9]\d*)）$", actual)
    return bool(match and actual.casefold() == collision_name(stem, int(match[1])).casefold())


def collision_name(base, number):
    suffix = f'（{number}）'
    return base[:MAX_FILENAME_STEM_LENGTH-len(suffix)].rstrip('. ') + suffix


def prepare_names(invoices):
    """Resolve file collisions before showing or exporting names, without moving files."""
    reserved = set()
    for invoice in invoices:
        if invoice.error or not invoice.name:
            invoice.pdf_name = ""
            continue
        base = pdf_stem(invoice.name)
        invoice.name = base
        parent = invoice.path.parent.resolve()
        origin = parent / invoice.path.name
        candidate = invoice.path.stem if matching_filename(invoice) else base
        number = 1
        while True:
            target = parent / (candidate + '.pdf')
            key = str(target).casefold()
            if key not in reserved and (not target.exists() or target == origin):
                break
            number += 1
            candidate = collision_name(base, number)
        invoice.pdf_name = candidate
        reserved.add(key)


def rename_pdfs(invoices, duplicates=()):
    """Rename selected originals to their already prepared names, without overwriting.

    Return updated duplicate references, successful renames and per-file failures.
    The application stages the workbook first and publishes it after all renames succeed.
    """
    jobs = [(i.path, i.pdf_name or pdf_stem(i.name)) for i in invoices if not i.error]
    updated, renamed, failures = {}, [], []
    for source, name in jobs:
        try:
            # Both resolved parents stay in the source file's own directory.
            parent = source.parent.resolve()
            origin = parent / source.name
            if not origin.is_file():
                raise FileNotFoundError("原文件不存在")
            if name != pdf_stem(name):
                raise ValueError("请先确认规范化后的 PDF 文件名")
            target = parent / (name + '.pdf')
            if target.parent != parent:
                raise ValueError("重命名目标必须位于原文件夹")
            if origin == target:
                updated[source] = source
                continue
            if target.exists():
                raise FileExistsError(f"目标文件已存在：{target.name}，请重新确认品名")
            origin.rename(target)  # Windows refuses an existing destination.
            updated[source] = target
            renamed.append((source, target))
        except (OSError, ValueError) as exc:
            failures.append((source, str(exc)))
    for invoice in invoices:
        invoice.path = updated.get(invoice.path, invoice.path)
    duplicates = [(updated.get(duplicate, duplicate), updated.get(first, first)) for duplicate, first in duplicates]
    return duplicates, renamed, failures
