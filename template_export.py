"""Preserve the supplied XLSX package, replacing only its sample rows/styles."""
from __future__ import annotations

import copy
import math
import re
import unicodedata
import os
import tempfile
import xml.etree.ElementTree as ET
from decimal import Decimal
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
from invoice_core import prepare_names, rename_pdfs

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
N = {"s": NS}
ET.register_namespace("", NS)


def tag(name):
    return f"{{{NS}}}{name}"


def serialize(root):
    # ElementTree discards unused xmlns declarations; remove stale Ignorable lists.
    for element in root.iter():
        element.attrib.pop("{http://schemas.openxmlformats.org/markup-compatibility/2006}Ignorable", None)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def export_workbook(template, output, invoices):
    template, output = Path(template), Path(output)
    if output.suffix.lower() != ".xlsx":
        raise ValueError("导出文件的后缀必须为 .xlsx")
    if template.resolve() == output.resolve():
        raise ValueError("请另存为新文件，不能覆盖模板")
    if not invoices:
        raise ValueError("没有可导出的发票")
    if any(i.error or not i.number or not i.name or i.total is None for i in invoices):
        raise ValueError("请先补填或移除标记为待处理的文件")
    keys = [i.key for i in invoices]
    if len(keys) != len(set(keys)):
        raise ValueError("存在重复发票号码，请核对后移除重复项")
    with ZipFile(template) as src:
        workbook = ET.fromstring(src.read("xl/workbook.xml"))
        relationships = ET.fromstring(src.read("xl/_rels/workbook.xml.rels"))
        sheet = workbook.find("s:sheets/s:sheet", N)
        rel_id = sheet.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        target = next(r.get("Target") for r in relationships if r.get("Id") == rel_id)
        sheet_path = target.lstrip("/") if target.startswith("/") else "xl/" + target
        root = ET.fromstring(src.read(sheet_path))
        styles = ET.fromstring(src.read("xl/styles.xml"))
        strings = []
        if "xl/sharedStrings.xml" in src.namelist():
            strings = ["".join(e.itertext()) for e in ET.fromstring(src.read("xl/sharedStrings.xml"))]
        data = root.find("s:sheetData", N)
        rows = list(data)
        header = rows[0]
        def cell_text(cell):
            if cell.get("t") == "s":
                return strings[int(cell.findtext("s:v", namespaces=N))]
            return "".join(cell.itertext())
        labels = {re.sub(r"\d", "", c.get("r")): cell_text(c) for c in header}
        expected = dict(zip("ABCDEFGH", ("姓名", "学号", "发票号码", "品名", "单价", "数量", "总价", "备注")))
        if labels != expected:
            raise ValueError("模板首行应依次为：姓名、学号、发票号码、品名、单价、数量、总价、备注")
        style_ids = {re.sub(r"\d", "", c.get("r")): c.get("s", "0") for c in rows[1]}
        xfs = styles.find("s:cellXfs", N)
        for col in "BCDEG":
            xf = copy.deepcopy(xfs[int(style_ids[col])])
            xf.set("numFmtId", "2" if col in "EG" else "49" if col in "BC" else "0")
            xf.set("applyNumberFormat", "1")
            alignment = xf.find("s:alignment", N)
            if alignment is None:
                alignment = ET.SubElement(xf, tag("alignment"))
            if col == "D":
                alignment.set("wrapText", "1")
            if col in "BC":
                alignment.set("shrinkToFit", "1")
            style_ids[col] = str(len(xfs))
            xfs.append(xf)
        xfs.set("count", str(len(xfs)))
        for row in rows[1:]:
            data.remove(row)
        old_merges = root.find("s:mergeCells", N)
        if old_merges is not None:
            root.remove(old_merges)
        width = 16.2
        for col in root.findall("s:cols/s:col", N):
            if int(col.get("min")) <= 4 <= int(col.get("max")):
                width = float(col.get("width"))
        for index, invoice in enumerate(invoices, 2):
            text_width = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in invoice.name)
            lines = math.ceil(text_width / max(5, width - 1))
            # Word wrapping can move a complete model token to the next line.
            height = 18 * max(1, lines + (1 if lines > 1 else 0))
            row = ET.SubElement(data, tag("row"), {"r": str(index), "ht": str(height), "customHeight": "1"})
            values = ["", "", invoice.number,
                      invoice.name, invoice.total, 1, invoice.total, ""]
            for col, value in zip("ABCDEFGH", values):
                cell = ET.SubElement(row, tag("c"), {"r": f"{col}{index}", "s": style_ids[col]})
                if value == "":
                    continue
                if isinstance(value, (Decimal, int)):
                    ET.SubElement(cell, tag("v")).text = str(value)
                else:
                    cell.set("t", "inlineStr")
                    ET.SubElement(ET.SubElement(cell, tag("is")), tag("t")).text = value
        end = len(invoices) + 1
        root.find("s:dimension", N).set("ref", f"A1:H{end}")
        for selection in root.findall("s:sheetViews/s:sheetView/s:selection", N):
            selection.set("activeCell", "A1")
            selection.set("sqref", "A1")
        if end > 2:
            merges = ET.Element(tag("mergeCells"), {"count": "2"})
            for col in "AB":
                ET.SubElement(merges, tag("mergeCell"), {"ref": f"{col}2:{col}{end}"})
            root.insert(list(root).index(data) + 1, merges)
        changes = {sheet_path: serialize(root), "xl/styles.xml": serialize(styles)}
        # Build in memory before opening the destination, so template errors never truncate it.
        import io
        buf = io.BytesIO()
        with ZipFile(buf, "w", ZIP_DEFLATED) as dest:
            for info in src.infolist():
                dest.writestr(info, changes.get(info.filename, src.read(info.filename)))
        output.write_bytes(buf.getvalue())
    return output


def export_bundle(template, output, invoices, duplicates=()):
    """Publish the workbook only when all PDF renames/copies succeed."""
    output = Path(output)
    if output.suffix.lower() != '.xlsx' or Path(template).resolve() == output.resolve():
        raise ValueError('请另存为新的 .xlsx 文件，不能覆盖模板')
    prepare_names(invoices)
    original_paths = [(invoice, invoice.path) for invoice in invoices]
    fd, temp_name = tempfile.mkstemp(prefix='.报销清单_', suffix='.xlsx', dir=output.parent)
    os.close(fd)
    staged = Path(temp_name)
    operations = []
    try:
        export_workbook(template, staged, invoices)
        updated_duplicates, operations, failures = rename_pdfs(invoices, duplicates)
        if failures:
            raise OSError('\n'.join(f'{path.name}：{reason}' for path, reason in failures))
        staged.replace(output)
        return updated_duplicates, operations
    except Exception as exc:
        rollback_errors = []
        for source, target, action in reversed(operations):
            try:
                if action == 'copy':
                    target.unlink(missing_ok=True)
                else:
                    if source.exists():
                        raise FileExistsError(f'原路径已被占用：{source}')
                    target.rename(source)
            except OSError as rollback_error:
                rollback_errors.append(str(rollback_error))
        for invoice, old_path in original_paths:
            if old_path.exists():
                invoice.path = old_path
        if rollback_errors:
            raise OSError(str(exc) + '\n部分文件未能恢复：\n' + '\n'.join(rollback_errors)) from exc
        raise
    finally:
        staged.unlink(missing_ok=True)
