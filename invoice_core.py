"""Offline PDF invoice extraction and name normalization."""
from __future__ import annotations

import hashlib
import re
import shutil
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path


MAX_PRODUCT_NAME_LENGTH = 60
SOFT_SEGMENT_LIMIT = 50
NAME_PUNCTUATION = "、，,；;。！!？?：:／/｜|"
PROBLEM_INVOICE_FOLDER = "存在问题的发票"


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
    destination_folder: Path | None = None
    buyer_name: str = ""
    buyer_tax_id: str = ""
    units: tuple[str, ...] = ()
    red_reasons: tuple[str, ...] = ()
    red_override: bool = False
    risk: str = "normal"
    risk_note: str = ""

    @property
    def key(self):
        return self.code, self.number


@dataclass(frozen=True)
class ProblemArchiveResult:
    folder: Path
    moved: tuple[tuple[Path, Path], ...] = ()
    already_present: tuple[Path, ...] = ()
    failures: tuple[tuple[Path, str], ...] = ()

    @property
    def archived_sources(self):
        return tuple(source for source, _ in self.moved) + self.already_present


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
    return re.sub(r"【[^】]*(?:】|$)|\[[^\]]*(?:\]|$)", "", value)


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


def _same_row(left, right):
    return abs(left["y"] - right["y"]) < max(left["h"], right["h"]) * .7


def buyer_from_page(entries):
    """Extract the buyer name and tax ID from the left half of an invoice."""
    if not entries:
        return "", ""
    page_right = max(e["x1"] for e in entries)
    name_labels = sorted((e for e in entries
                          if compact(e["text"]).replace("：", ":").startswith("名称:")),
                         key=lambda e: e["x0"])
    buyer_label = next((e for e in name_labels if e["x0"] < page_right * .5), None)
    if buyer_label is None:
        return "", ""
    seller_x = min(next((e["x0"] for e in name_labels if e["x0"] > buyer_label["x0"]), page_right * .5),
                   page_right * .5)
    label_text = buyer_label["text"].replace("：", ":")
    inline_name = label_text.split(":", 1)[1].strip() if ":" in label_text else ""
    name_parts = [e["text"].strip() for e in entries
                  if e is not buyer_label and _same_row(e, buyer_label)
                  and e["x0"] >= buyer_label["x1"] - buyer_label["h"] * 1.2 and e["x0"] < seller_x]
    buyer_name = compact(inline_name + "".join(name_parts))

    tax_labels = sorted((e for e in entries if "统一社会信用代码/纳税人识别号" in compact(e["text"])),
                        key=lambda e: e["x0"])
    tax_label = next((e for e in tax_labels if e["x0"] < page_right * .5), None)
    buyer_tax_id = ""
    if tax_label:
        tax_parts = [e["text"].strip() for e in entries
                     if e is not tax_label and _same_row(e, tax_label)
                     and e["x0"] >= tax_label["x1"] - tax_label["h"] * 1.2 and e["x0"] < seller_x]
        buyer_tax_id = compact("".join(tax_parts))
    return buyer_name, buyer_tax_id


def units_from_page(entries):
    """Return item units from the unit column, excluding headings and totals."""
    rows = group_rows(entries)
    unit_header = next((e for e in entries if compact(e["text"]) == "单位"), None)
    if unit_header is None:
        return []
    quantity_header = next((e for e in entries if compact(e["text"]) == "数量"
                            and abs(e["y"] - unit_header["y"]) < unit_header["h"] * 2), None)
    right = quantity_header["x0"] - unit_header["h"] * .5 if quantity_header else unit_header["x1"] + unit_header["h"] * 5
    end = min((r["y"] for r in rows if r["y"] > unit_header["y"] and
               ("合计" in compact(r["text"]) or compact(r["text"]).startswith("备注"))), default=float("inf"))
    units = []
    for row in rows:
        if not (unit_header["y"] + unit_header["h"] * .5 < row["y"] < end):
            continue
        value = compact("".join(e["text"] for e in row["entries"]
                                if e["x0"] >= unit_header["x0"] - unit_header["h"] and e["x0"] < right))
        if value and re.fullmatch(r"[\u4e00-\u9fffA-Za-z]{1,8}", value) and value not in units:
            units.append(value)
    return units


OFFICE_PATTERN = re.compile(
    r"打印纸|复印纸|纸张|中性笔|签字笔|圆珠笔|铅笔|钢笔|马克笔|记号笔|荧光笔|笔芯|"
    r"笔记本(?!电脑)|图书|书籍|教材|墨盒|碳粉|硒鼓"
)
EQUIPMENT_PATTERN = re.compile(
    r"电脑整机|计算机整机|笔记本电脑|台式电脑|台式机|电脑主机|计算机主机|工作站|服务器|"
    r"3D打印机|三维打印机|打印机整机|复印机|扫描仪|投影仪|数控机床|整机", re.I
)


def goods_type(invoice):
    text = compact(invoice.name + "\n" + invoice.original_names)
    # Printer consumables are office supplies even though their names contain 打印机.
    if OFFICE_PATTERN.search(text):
        return "办公"
    if EQUIPMENT_PATTERN.search(text) or text in {"电脑", "计算机", "打印机", "机器", "设备"}:
        return "设备"
    return "材料"


def buyer_is_person(invoice):
    if "个人" in invoice.buyer_name:
        return True
    if invoice.buyer_tax_id:
        return False
    organization_words = "公司|大学|学院|学校|中心|研究院|医院|政府|委员会|协会|合作社|事务所|集团|厂|店"
    return bool(re.fullmatch(r"[\u4e00-\u9fff]{2,4}", invoice.buyer_name)
                and not re.search(organization_words, invoice.buyer_name))


def evaluate_invoice(invoice, expense_type="材料"):
    if expense_type not in {"材料", "办公", "设备"}:
        raise ValueError("报销类型必须是材料、办公或设备")
    reasons = []
    if buyer_is_person(invoice):
        reasons.append("购买方为个人")
    if "批" in invoice.units:
        reasons.append("单位为批")
    actual_type = goods_type(invoice)
    if actual_type != expense_type:
        reasons.append(f"品名属于{actual_type}，与{expense_type}类型不符")
    invoice.red_reasons = tuple(reasons)
    if reasons and not invoice.red_override:
        invoice.risk, invoice.risk_note = "red", "；".join(reasons)
    elif invoice.total is not None and invoice.total > Decimal("2000"):
        invoice.risk, invoice.risk_note = "blue", "需要公对公转账"
    elif invoice.total is not None and Decimal("500") <= invoice.total < Decimal("2000"):
        invoice.risk, invoice.risk_note = "yellow", "需要支付凭证"
    else:
        invoice.risk, invoice.risk_note = "normal", ""
    return invoice


def evaluate_invoices(invoices, expense_type="材料"):
    for invoice in invoices:
        evaluate_invoice(invoice, expense_type)
    order = {"red": 0, "blue": 1, "yellow": 2, "normal": 4}
    return sorted(invoices, key=lambda invoice: order[invoice.risk] if not invoice.error else 3)


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
    buyers = [buyer_from_page(page) for page in pages]
    invoice.buyer_name = next((name for name, _ in buyers if name), "")
    invoice.buyer_tax_id = next((tax_id for _, tax_id in buyers if tax_id), "")
    invoice.units = tuple(dict.fromkeys(unit for page in pages for unit in units_from_page(page)))
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


def deduplicate_invoices(read_invoices):
    invoices, seen, duplicates = [], {}, []
    for invoice in read_invoices:
        path = invoice.path
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
            if not invoice.error.startswith("相同发票号码的内容不一致"):
                invoice.error = "相同发票号码的内容不一致，请核对后移除重复项；" + invoice.error
        elif invoice.number:
            seen[invoice.key] = invoice
        invoices.append(invoice)
    return invoices, duplicates


def scan_files(files, progress=None):
    files = [Path(path) for path in files]
    files = list(dict.fromkeys(path.resolve() for path in files
                               if path.is_file() and path.suffix.lower() == ".pdf"))
    if not files:
        raise ValueError("没有可读取的 PDF 文件")
    read_invoices = []
    for i, path in enumerate(files, 1):
        if progress:
            progress(i, len(files), path.name)
        read_invoices.append(read_invoice(path))
    invoices, duplicates = deduplicate_invoices(read_invoices)
    prepare_names(invoices)
    return invoices, duplicates


def scan_folder(folder, recursive=False, progress=None):
    folder = Path(folder)
    if not folder.is_dir():
        raise ValueError("请选择存在的发票文件夹")
    files = sorted((p for p in (folder.rglob("*") if recursive else folder.iterdir())
                    if p.is_file() and p.suffix.lower() == ".pdf"), key=lambda p: str(p.relative_to(folder)).casefold())
    if not files:
        raise ValueError("文件夹中没有 PDF 文件")
    return scan_files(files, progress)


def shorten_product_name(value):
    """Limit the product-name body while preferring complete punctuation-delimited phrases."""
    value = value.rstrip('. ')
    if len(value) <= MAX_PRODUCT_NAME_LENGTH:
        return value

    punctuation_positions = [index for index, char in enumerate(value) if char in NAME_PUNCTUATION]
    # If the next complete punctuation-delimited phrase would push the accumulated
    # content past 50 characters, omit its leading punctuation and everything after it.
    for first, second in zip(punctuation_positions, punctuation_positions[1:]):
        if second > SOFT_SEGMENT_LIMIT:
            prefix = value[:first].rstrip(NAME_PUNCTUATION + ' ')
            if prefix and len(prefix) < MAX_PRODUCT_NAME_LENGTH:
                return prefix + '等'
            break

    content_limit = MAX_PRODUCT_NAME_LENGTH - 1  # Reserve one character for “等”.
    useful_breaks = [index for index in punctuation_positions if 30 <= index <= content_limit]
    whitespace_breaks = [match.start() for match in re.finditer(r'\s+', value)
                         if 30 <= match.start() <= content_limit]
    cut = max(useful_breaks + whitespace_breaks, default=content_limit)

    # Do not split a contiguous Latin model/serial token when a nearby boundary exists.
    if (cut == content_limit and cut < len(value)
            and re.fullmatch(r'[A-Za-z0-9._-]{2}', value[cut - 1:cut + 1])):
        token_start = cut - 1
        while token_start > 0 and re.match(r'[A-Za-z0-9._-]', value[token_start - 1]):
            token_start -= 1
        if token_start >= 30:
            cut = token_start
    prefix = value[:cut].rstrip(NAME_PUNCTUATION + ' ')
    return (prefix or value[:content_limit]).rstrip('. ') + '等'


def pdf_stem(name):
    """Normalize a product name so it can also serve as a Windows PDF stem."""
    substitutions = str.maketrans({'<': '＜', '>': '＞', ':': '：', '"': '＂', '/': '／',
                                  '\\': '＼', '|': '｜', '?': '？', '*': '×'})
    stem = remove_square_notes(name).replace('价外费用', '')
    stem = re.sub(r'\s*[+＋]\s*', '和', stem)
    stem = re.sub(r'\s+', ' ', stem).translate(substitutions)
    stem = re.sub(r'[\x00-\x1f]', '', stem).strip().rstrip('. ')
    stem = shorten_product_name(normalize_name(stem))
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
    match = re.search(r"_([2-9]\d*)$", actual)
    return bool(match and actual.casefold() == collision_name(stem, int(match[1])).casefold())


def collision_name(base, number):
    return f'{shorten_product_name(base)}_{number}'


def _file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.digest()


def _same_file_content(source, target):
    source, target = Path(source), Path(target)
    return (source.stat().st_size == target.stat().st_size
            and _file_sha256(source) == _file_sha256(target))


def _problem_target(folder, source, reserved):
    suffix = source.suffix or '.pdf'
    stem = source.stem
    candidate = source.name
    number = 2
    while candidate.casefold() in reserved or (folder / candidate).exists():
        candidate = f'{stem}_{number}{suffix}'
        number += 1
    reserved.add(candidate.casefold())
    return folder / candidate


def send_to_recycle_bin(paths):
    """Send existing paths to the Windows Recycle Bin without a permanent-delete fallback."""
    sources = tuple(str(Path(path).resolve()) for path in paths)
    if not sources:
        return
    if sys.platform != 'win32':
        raise OSError('当前系统不支持 Windows 回收站')

    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = (
            ('hwnd', wintypes.HWND),
            ('wFunc', wintypes.UINT),
            ('pFrom', wintypes.LPCWSTR),
            ('pTo', wintypes.LPCWSTR),
            ('fFlags', ctypes.c_ushort),
            ('fAnyOperationsAborted', wintypes.BOOL),
            ('hNameMappings', ctypes.c_void_p),
            ('lpszProgressTitle', wintypes.LPCWSTR),
        )

    source_list = '\0'.join(sources) + '\0\0'
    operation = SHFILEOPSTRUCTW()
    operation.wFunc = 3  # FO_DELETE
    operation.pFrom = source_list
    operation.fFlags = 0x0040 | 0x0010 | 0x0004 | 0x0400  # ALLOWUNDO, NOCONFIRMATION, SILENT, NOERRORUI
    shell32 = ctypes.windll.shell32
    shell32.SHFileOperationW.argtypes = (ctypes.POINTER(SHFILEOPSTRUCTW),)
    shell32.SHFileOperationW.restype = ctypes.c_int
    result = shell32.SHFileOperationW(ctypes.byref(operation))
    if result or operation.fAnyOperationsAborted:
        reason = f'Windows 回收站操作失败（错误代码 {result}）' if result else 'Windows 回收站操作已取消'
        raise OSError(reason)


def archive_problem_pdfs(sources, selected_folder, recycler=None, copier=shutil.copy2):
    """Copy red invoices to a problem folder, verify them, then recycle the originals."""
    selected_folder = Path(selected_folder).resolve()
    if not selected_folder.is_dir():
        raise FileNotFoundError(f'读取文件夹不存在：{selected_folder}')
    problem_folder = selected_folder / PROBLEM_INVOICE_FOLDER
    problem_folder.mkdir(exist_ok=True)
    if not problem_folder.is_dir():
        raise NotADirectoryError(f'无法创建问题发票文件夹：{problem_folder}')
    problem_folder = problem_folder.resolve()

    resolved_sources = tuple(Path(path).resolve() for path in sources)
    if not resolved_sources:
        return ProblemArchiveResult(problem_folder)
    source_keys = [str(path).casefold() for path in resolved_sources]
    if len(set(source_keys)) != len(source_keys):
        return ProblemArchiveResult(problem_folder, failures=((resolved_sources[0], '同一原文件出现多次，未执行移动'),))

    already_present, pending = [], []
    reserved = {item.name.casefold() for item in problem_folder.iterdir()}
    created = []
    try:
        for source in resolved_sources:
            if not source.is_file():
                raise FileNotFoundError(f'原文件不存在：{source}')
            if source.parent == problem_folder:
                already_present.append(source)
                continue
            target = _problem_target(problem_folder, source, reserved)
            copier(source, target)
            created.append(target)
            pending.append((source, target))

        if len({str(target).casefold() for _, target in pending}) != len(pending):
            raise OSError('副本目标未能保持一一对应')
        for source, target in pending:
            if not target.is_file() or not _same_file_content(source, target):
                raise OSError(f'副本校验失败：{source.name}')
    except (OSError, ValueError) as exc:
        cleanup_errors = []
        for target in created:
            try:
                target.unlink(missing_ok=True)
            except OSError as cleanup_exc:
                cleanup_errors.append(f'{target.name}: {cleanup_exc}')
        detail = str(exc)
        if cleanup_errors:
            detail += '；未能清理副本：' + '；'.join(cleanup_errors)
        failed = resolved_sources[0] if not pending else pending[-1][0]
        return ProblemArchiveResult(problem_folder, already_present=tuple(already_present),
                                    failures=((failed, detail),))

    recycle_error = ''
    try:
        (recycler or send_to_recycle_bin)([source for source, _ in pending])
    except OSError as exc:
        recycle_error = str(exc)

    moved, failures = [], []
    for source, target in pending:
        if not source.exists():
            moved.append((source, target))
            continue
        detail = recycle_error or '原文件仍在原位置，未确认进入回收站'
        try:
            target.unlink(missing_ok=True)
        except OSError as cleanup_exc:
            detail += f'；未能清理副本：{cleanup_exc}'
        failures.append((source, detail))
    return ProblemArchiveResult(problem_folder, tuple(moved), tuple(already_present), tuple(failures))


def prepare_names(invoices):
    """Resolve file collisions before showing or exporting names, without moving files."""
    reserved = set()
    for invoice in invoices:
        if invoice.error or not invoice.name:
            invoice.pdf_name = ""
            continue
        base = pdf_stem(invoice.name)
        invoice.name = base
        parent = (invoice.destination_folder or invoice.path.parent).resolve()
        origin = invoice.path.resolve()
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
    """Rename local PDFs or copy external PDFs into their destination folder.

    Return updated duplicate references, successful operations and per-file failures.
    The application stages the workbook first and publishes it after all renames succeed.
    """
    jobs = [(i.path, i.destination_folder, i.pdf_name or pdf_stem(i.name)) for i in invoices if not i.error]
    updated, operations, failures = {}, [], []
    for source, destination_folder, name in jobs:
        try:
            origin = source.resolve()
            parent = (destination_folder or source.parent).resolve()
            if not origin.is_file():
                raise FileNotFoundError("原文件不存在")
            if name != pdf_stem(name):
                raise ValueError("请先确认规范化后的 PDF 文件名")
            target = parent / (name + '.pdf')
            if target.parent != parent:
                raise ValueError("PDF 目标路径无效")
            if origin == target:
                updated[source] = source
                continue
            if target.exists():
                raise FileExistsError(f"目标文件已存在：{target.name}，请重新确认品名")
            if origin.parent == parent:
                origin.rename(target)  # Windows refuses an existing destination.
                action = "rename"
            else:
                shutil.copy2(origin, target)
                action = "copy"
            updated[source] = target
            operations.append((source, target, action))
        except (OSError, ValueError) as exc:
            failures.append((source, str(exc)))
    for invoice in invoices:
        invoice.path = updated.get(invoice.path, invoice.path)
    duplicates = [(updated.get(duplicate, duplicate), updated.get(first, first)) for duplicate, first in duplicates]
    return duplicates, operations, failures
