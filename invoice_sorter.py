"""PDF 发票按价税合计金额排序并分组整理。

金额识别直接复用 invoice_core.py 中的 read_invoice()：
- 优先读取 PDF 文本层；
- 文本不足时自动使用 RapidOCR；
- 价税合计只取“（小写）”后面的金额。

请将本文件与原程序的 invoice_core.py 放在同一目录。
"""
from __future__ import annotations

import csv
import os
import queue
import re
import shutil
import threading
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from invoice_core import read_invoice


@dataclass
class SortItem:
    path: Path
    total: Decimal | None
    engine: str = ""
    reason: str = ""


def safe_stem(value: str) -> str:
    """生成可用于 Windows 文件名的字符串。"""
    value = str(value).translate(str.maketrans({
        '<': '＜', '>': '＞', ':': '：', '"': '＂',
        '/': '／', '\\': '＼', '|': '｜', '?': '？', '*': '×'
    }))
    value = re.sub(r'[\x00-\x1f]', '', value).strip().rstrip('. ')
    return value or "未命名"


def unique_path(path: Path) -> Path:
    """目标已存在时增加（2）、（3）……，不覆盖任何文件。"""
    if not path.exists():
        return path
    for index in range(2, 10000):
        candidate = path.with_name(f"{path.stem}（{index}）{path.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"无法为文件生成不重复名称：{path.name}")


def find_pdfs(folder: Path, recursive: bool) -> list[Path]:
    iterator = folder.rglob("*") if recursive else folder.iterdir()
    return sorted(
        (p for p in iterator if p.is_file() and p.suffix.lower() == ".pdf"),
        key=lambda p: str(p.relative_to(folder)).casefold(),
    )


def read_amount(pdf_path: Path) -> SortItem:
    """完全复用旧报销程序的金额识别结果，只关心 invoice.total。"""
    invoice = read_invoice(pdf_path)
    if invoice.total is not None:
        return SortItem(
            path=pdf_path,
            total=invoice.total,
            engine=invoice.engine or "",
            reason="",
        )

    # invoice.error 可能还包含号码、品名等错误；这里仅作为诊断文本保留。
    reason = invoice.error or "未识别价税合计行中（小写）后的金额"
    return SortItem(
        path=pdf_path,
        total=None,
        engine=invoice.engine or "",
        reason=reason,
    )


def group_folder_name(group_no: int, first_no: int, last_no: int,
                      min_total: Decimal, max_total: Decimal,
                      group_total: Decimal) -> str:
    """固定宽度序号保证资源管理器按名称排序时顺序稳定，并标注本组总金额。"""
    return (
        f"{group_no:03d}_"
        f"第{first_no:04d}-{last_no:04d}张_"
        f"{min_total:.2f}-{max_total:.2f}元_"
        f"合计{group_total:.2f}元"
    )


def split_groups_by_500(items: list[SortItem], group_size: int, descending: bool = False) -> list[list[SortItem]]:
    """
    每组最多 group_size 张，并禁止同一文件夹同时包含 <500 与 >=500 的发票。

    对按金额升序排列的列表，这等价于：
    - 先将 <500 元发票每 group_size 张分组；
    - 若临界组不足 group_size 张，>=500 元发票不补入该组，而从下一组开始；
    - 再将 >=500 元发票每 group_size 张分组。

    这样可以确保跨越 500 元边界时，>=500 元发票整体顺延到下一文件夹。
    """
    threshold = Decimal("500.00")
    below = [item for item in items if item.total is not None and item.total < threshold]
    at_or_above = [item for item in items if item.total is not None and item.total >= threshold]

    groups: list[list[SortItem]] = []
    categories = (at_or_above, below) if descending else (below, at_or_above)
    for category in categories:
        for start in range(0, len(category), group_size):
            groups.append(category[start:start + group_size])
    return groups


def organize_items(items: list[SortItem], output_dir: Path,
                   group_size: int = 15, descending: bool = False,
                   prefix_filename: bool = True) -> tuple[int, int, Path]:
    recognized = [item for item in items if item.total is not None]
    unknown = [item for item in items if item.total is None]

    recognized.sort(
        key=lambda item: (item.total, item.path.name.casefold()),
        reverse=descending,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_rows: list[dict[str, str]] = []

    groups = split_groups_by_500(recognized, group_size, descending)
    order_no = 0

    for group_no, group in enumerate(groups, 1):
        first_no = order_no + 1
        last_no = order_no + len(group)
        totals = [item.total for item in group if item.total is not None]
        group_total = sum(totals, Decimal(0))
        folder_name = group_folder_name(
            group_no, first_no, last_no, min(totals), max(totals), group_total
        )
        target_folder = output_dir / safe_stem(folder_name)
        target_folder.mkdir(parents=True, exist_ok=True)

        for item in group:
            order_no += 1
            if prefix_filename:
                new_name = (
                    f"{order_no:04d}_{item.total:.2f}元_"
                    f"{safe_stem(item.path.stem)}.pdf"
                )
            else:
                new_name = safe_stem(item.path.stem) + ".pdf"

            destination = unique_path(target_folder / new_name)
            shutil.copy2(item.path, destination)

            manifest_rows.append({
                "排序序号": str(order_no),
                "分组序号": str(group_no),
                "金额": f"{item.total:.2f}",
                "文件夹总金额": f"{group_total:.2f}",
                "识别方式": item.engine,
                "原文件": str(item.path),
                "整理后文件": str(destination),
                "状态": "已整理",
                "说明": "",
            })

    if unknown:
        unknown_folder = output_dir / "999_未识别金额"
        unknown_folder.mkdir(parents=True, exist_ok=True)
        for item in unknown:
            destination = unique_path(unknown_folder / item.path.name)
            shutil.copy2(item.path, destination)
            manifest_rows.append({
                "排序序号": "",
                "分组序号": "",
                "金额": "",
                "文件夹总金额": "",
                "识别方式": item.engine,
                "原文件": str(item.path),
                "整理后文件": str(destination),
                "状态": "金额未识别",
                "说明": item.reason,
            })

    manifest_path = output_dir / "发票整理清单.csv"
    with manifest_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "排序序号", "分组序号", "金额", "文件夹总金额", "识别方式",
                "原文件", "整理后文件", "状态", "说明",
            ],
        )
        writer.writeheader()
        writer.writerows(manifest_rows)

    return len(recognized), len(unknown), manifest_path


class App:
    """Reusable amount-sorting panel; it can also run as a standalone window."""

    def __init__(self, root: tk.Tk, parent=None, standalone: bool = True):
        self.root = root
        self.standalone = standalone
        self.messages: queue.Queue = queue.Queue()
        self.busy = False
        self.items: list[SortItem] = []
        self.closed = False
        self.poll_job = None

        self.folder = tk.StringVar()
        self.output = tk.StringVar()
        self.recursive = tk.BooleanVar(value=False)
        self.descending = tk.BooleanVar(value=False)
        self.prefix_filename = tk.BooleanVar(value=True)
        self.group_size = tk.StringVar(value="15")
        self.status = tk.StringVar(value="选择发票文件夹后开始读取。")

        if standalone:
            root.title("PDF发票按金额排序整理")
            root.geometry("980x650")
            root.minsize(860, 560)
            root.protocol("WM_DELETE_WINDOW", self.close)

        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", font=("Microsoft YaHei UI", 10))
        style.configure("Treeview", rowheight=30)

        frame = ttk.Frame(parent or root, padding=20)
        frame.pack(fill="both", expand=True)
        self.frame = frame
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(4, weight=1)

        top = ttk.Frame(frame)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="发票文件夹").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=5)
        ttk.Entry(top, textvariable=self.folder).grid(row=0, column=1, sticky="ew", pady=5)
        ttk.Button(top, text="选择", command=self.choose_folder).grid(row=0, column=2, padx=(10, 0), pady=5)

        ttk.Label(top, text="输出文件夹").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=5)
        ttk.Entry(top, textvariable=self.output).grid(row=1, column=1, sticky="ew", pady=5)
        ttk.Button(top, text="选择", command=self.choose_output).grid(row=1, column=2, padx=(10, 0), pady=5)

        options = ttk.Frame(frame)
        options.grid(row=1, column=0, sticky="ew", pady=(10, 8))
        ttk.Checkbutton(options, text="包含子文件夹", variable=self.recursive).pack(side="left")
        ttk.Checkbutton(options, text="金额从大到小", variable=self.descending).pack(side="left", padx=(18, 0))
        ttk.Checkbutton(options, text="文件名前加排序序号和金额", variable=self.prefix_filename).pack(side="left", padx=(18, 0))
        ttk.Label(options, text="每组").pack(side="left", padx=(18, 4))
        ttk.Entry(options, textvariable=self.group_size, width=5).pack(side="left")
        ttk.Label(options, text="张").pack(side="left", padx=(4, 0))

        actions = ttk.Frame(frame)
        actions.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        self.scan_button = ttk.Button(actions, text="读取并预览", command=self.start_scan)
        self.scan_button.pack(side="left")
        self.organize_button = ttk.Button(actions, text="按金额整理", command=self.start_organize)
        self.organize_button.pack(side="left", padx=8)
        self.open_button = ttk.Button(actions, text="打开输出文件夹", command=self.open_output)
        self.open_button.pack(side="left")

        self.progress = ttk.Progressbar(frame, mode="determinate")
        self.progress.grid(row=3, column=0, sticky="ew", pady=(0, 8))

        table_frame = ttk.Frame(frame)
        table_frame.grid(row=4, column=0, sticky="nsew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        self.table = ttk.Treeview(
            table_frame,
            columns=("status", "amount", "engine", "file"),
            show="headings",
        )
        for key, title, width, anchor in (
            ("status", "状态", 100, "w"),
            ("amount", "价税合计", 120, "e"),
            ("engine", "识别方式", 100, "w"),
            ("file", "PDF 文件", 570, "w"),
        ):
            self.table.heading(key, text=title)
            self.table.column(key, width=width, anchor=anchor, stretch=(key == "file"))
        yscroll = ttk.Scrollbar(table_frame, command=self.table.yview)
        self.table.configure(yscrollcommand=yscroll.set)
        self.table.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")

        ttk.Label(frame, textvariable=self.status, wraplength=920).grid(
            row=5, column=0, sticky="w", pady=(10, 0)
        )

        self.set_busy(False)
        self.poll_job = root.after(100, self.poll)

    def shutdown(self):
        self.closed = True
        if self.poll_job is not None:
            try:
                self.root.after_cancel(self.poll_job)
            except tk.TclError:
                pass
            self.poll_job = None

    def close(self):
        self.shutdown()
        if self.standalone:
            self.root.destroy()

    def choose_folder(self):
        path = filedialog.askdirectory(title="选择存放 PDF 发票的文件夹")
        if not path:
            return
        self.folder.set(path)
        if not self.output.get():
            self.output.set(str(Path(path).parent / (Path(path).name + "_按金额整理")))
        self.start_scan()

    def choose_output(self):
        path = filedialog.askdirectory(title="选择输出文件夹")
        if path:
            self.output.set(path)

    def handle_drop_paths(self, raw_paths):
        """Add dropped PDFs to the sorting preview without scanning their sibling files."""
        if self.busy:
            return
        existing = {item.path.resolve() for item in self.items}
        paths = []
        for raw in raw_paths:
            path = Path(raw)
            if path.is_file() and path.suffix.lower() == ".pdf":
                resolved = path.resolve()
                if resolved not in existing and resolved not in paths:
                    paths.append(resolved)
        if not paths:
            self.status.set("拖入内容中没有新的 PDF 文件。")
            return
        if not self.folder.get():
            self.folder.set(str(paths[0].parent))
        if not self.output.get():
            source = Path(self.folder.get())
            self.output.set(str(source.parent / (source.name + "_按金额整理")))
        self.set_busy(True)

        def work():
            try:
                results = []
                for index, path in enumerate(paths, 1):
                    self.messages.put(("progress", (index, len(paths), path.name)))
                    results.append(read_amount(path))
                self.messages.put(("drop_done", results))
            except Exception as exc:
                self.messages.put(("error", str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def set_busy(self, busy: bool):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for widget in (self.scan_button, self.organize_button, self.open_button):
            widget.configure(state=state)

    def start_scan(self):
        if self.busy:
            return
        folder = Path(self.folder.get().strip())
        if not folder.is_dir():
            messagebox.showerror("路径错误", "请选择存在的发票文件夹。")
            return

        self.items = []
        self.table.delete(*self.table.get_children())
        self.set_busy(True)
        self.status.set("正在读取 PDF……")
        recursive = self.recursive.get()

        def work():
            try:
                files = find_pdfs(folder, recursive)
                if not files:
                    raise ValueError("文件夹中没有 PDF 文件")
                results = []
                for index, path in enumerate(files, 1):
                    self.messages.put(("progress", (index, len(files), path.name)))
                    results.append(read_amount(path))
                self.messages.put(("scan_done", results))
            except Exception as exc:
                self.messages.put(("error", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def refresh(self):
        self.table.delete(*self.table.get_children())
        display_items = sorted(
            self.items,
            key=lambda item: (
                item.total is None,
                item.total if item.total is not None else Decimal(0),
                item.path.name.casefold(),
            ),
            reverse=False,
        )
        if self.descending.get():
            known = [x for x in display_items if x.total is not None]
            unknown = [x for x in display_items if x.total is None]
            display_items = list(reversed(known)) + unknown

        for item in display_items:
            status = "已识别" if item.total is not None else "未识别金额"
            amount = f"{item.total:.2f}" if item.total is not None else ""
            self.table.insert("", "end", values=(status, amount, item.engine, item.path.name))

        recognized = sum(item.total is not None for item in self.items)
        unknown = len(self.items) - recognized
        total = sum((item.total for item in self.items if item.total is not None), Decimal(0))
        self.status.set(
            f"共 {len(self.items)} 张；识别金额 {recognized} 张；"
            f"未识别 {unknown} 张；已识别金额合计 {total:,.2f} 元。"
        )

    def start_organize(self):
        if self.busy:
            return
        if not self.items:
            messagebox.showinfo("尚未读取", "请先读取发票。")
            return
        raw_output = self.output.get().strip()
        if not raw_output:
            messagebox.showerror("路径错误", "请选择输出文件夹。")
            return
        output = Path(raw_output)
        try:
            group_size = int(self.group_size.get())
            if group_size <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("数量错误", "每组数量必须为正整数。")
            return

        self.set_busy(True)
        self.status.set("正在复制并整理发票……")
        items = list(self.items)
        descending = self.descending.get()
        prefix_filename = self.prefix_filename.get()

        def work():
            try:
                result = organize_items(
                    items,
                    output,
                    group_size=group_size,
                    descending=descending,
                    prefix_filename=prefix_filename,
                )
                self.messages.put(("organize_done", (output, result)))
            except Exception as exc:
                self.messages.put(("error", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def open_output(self):
        path = Path(self.output.get().strip())
        if not path.is_dir():
            messagebox.showinfo("输出文件夹", "输出文件夹尚不存在。")
            return
        try:
            if os.name == "nt":
                os.startfile(path)
            else:
                import subprocess
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as exc:
            messagebox.showerror("无法打开", str(exc))

    def poll(self):
        try:
            while True:
                event, value = self.messages.get_nowait()
                if event == "progress":
                    index, total, filename = value
                    self.progress.configure(maximum=total, value=index - 1)
                    self.status.set(f"正在读取 {index}/{total}：{filename}")
                elif event == "scan_done":
                    self.items = value
                    self.progress.configure(value=self.progress["maximum"])
                    self.set_busy(False)
                    self.refresh()
                elif event == "drop_done":
                    self.items.extend(value)
                    self.progress.configure(value=self.progress["maximum"])
                    self.set_busy(False)
                    self.refresh()
                elif event == "organize_done":
                    output, result = value
                    recognized, unknown, manifest = result
                    self.set_busy(False)
                    self.status.set(
                        f"整理完成：{recognized} 张已按金额分组；{unknown} 张进入未识别金额文件夹。"
                    )
                    messagebox.showinfo(
                        "整理完成",
                        f"已整理：{recognized} 张\n"
                        f"未识别金额：{unknown} 张\n\n"
                        f"输出：{output}\n"
                        f"清单：{manifest}",
                    )
                elif event == "error":
                    self.set_busy(False)
                    self.status.set(value)
                    messagebox.showerror("处理失败", value)
        except queue.Empty:
            pass
        if not self.closed:
            self.poll_job = self.root.after(100, self.poll)


def main():
    root = tk.Tk()
    app = App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
