"""Double-click desktop application; no server or account needed."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import re
import sys
import threading
from datetime import datetime
from decimal import Decimal
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from invoice_core import money, scan_folder, prepare_names, pdf_stem
from template_export import export_bundle

BASE = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
RESOURCES = Path(getattr(sys, "_MEIPASS", BASE))


def default_template():
    external = BASE / "报销清单表.xlsx"
    return external if external.is_file() else RESOURCES / "报销清单表.xlsx"


class App:
    def __init__(self, root):
        self.root = root
        self.invoices = []
        self.duplicates = []
        self.messages = queue.Queue()
        self.busy = False
        self.last_output = None
        self.settings_path = BASE / "报销工具设置.json"
        try:
            settings = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            settings = {}
        self.last_folder = settings.get('folder', '')
        self.folder = tk.StringVar(value='')
        self.recursive = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="选择发票文件夹后开始读取。")
        root.title("发票报销工具")
        root.geometry("1020x660")
        root.minsize(850, 560)
        root.configure(bg="#f4f6f8")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure(".", font=("Microsoft YaHei UI", 10))
        style.configure("TFrame", background="#f4f6f8")
        style.configure("TLabel", background="#f4f6f8", foreground="#263445")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 21, "bold"))
        style.configure("Hint.TLabel", foreground="#576575")
        style.configure("TButton", padding=(14, 8))
        style.configure("Accent.TButton", background="#205ac2", foreground="white")
        style.map("Accent.TButton", background=[("disabled", "#b7c4d8"), ("active", "#164baf")])
        style.configure("Treeview", rowheight=33, font=("Microsoft YaHei UI", 10))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"), padding=(6, 9))
        frame = ttk.Frame(root, padding=24)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(3, weight=1)
        ttk.Label(frame, text="发票转报销清单", style="Title.TLabel").grid(row=0, column=0, sticky='w')
        top = ttk.Frame(frame)
        top.grid(row=1, column=0, sticky='ew', pady=(18, 12))
        top.columnconfigure(0, weight=1)
        choices = ttk.Frame(top)
        choices.grid(row=0, column=0, sticky='w')
        self.choose_button = ttk.Button(choices, text="选择文件夹并读取", style="Accent.TButton", command=self.choose_folder)
        self.choose_button.pack(side="left")
        self.reread_button = ttk.Button(choices, text="重新读取", command=self.start_scan)
        self.reread_button.pack(side="left", padx=8)
        self.recursive_check = ttk.Checkbutton(choices, text="包含子文件夹", variable=self.recursive)
        self.recursive_check.pack(side="left", padx=8)
        self.export_button = ttk.Button(top, text="确认并导出", style="Accent.TButton", command=self.export)
        self.export_button.grid(row=0, column=1, sticky='e', padx=(12, 0))
        self.folder_label = ttk.Label(frame, textvariable=self.folder, style='Hint.TLabel', wraplength=920)
        self.folder_label.grid(row=2, column=0, sticky='w', pady=(0, 12))
        self.folder_label.grid_remove()
        table_frame = ttk.Frame(frame)
        table_frame.grid(row=3, column=0, sticky='nsew')
        self.table = ttk.Treeview(table_frame, columns=("status", "number", "name", "total", "file"), show="headings", selectmode="extended")
        for key, title, width in (("status", "状态", 100), ("number", "发票号码", 210), ("name", "品名", 230),
                                  ("total", "含税合计", 95), ("file", "导出后的 PDF 文件名", 230)):
            self.table.heading(key, text=title)
            self.table.column(key, width=width, minwidth=70, anchor="e" if key == "total" else "w", stretch=key in ("name", "file"))
        yscroll = ttk.Scrollbar(table_frame, command=self.table.yview)
        xscroll = ttk.Scrollbar(table_frame, orient="horizontal", command=self.table.xview)
        self.table.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.table.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.table.tag_configure("error", foreground="#b4471f", background="#fff5ef")
        self.table.bind("<Double-1>", lambda _: self.edit_selected())
        actions = ttk.Frame(frame)
        actions.grid(row=4, column=0, sticky='ew', pady=12)
        self.edit_button = ttk.Button(actions, text="修改品名", command=self.edit_selected)
        self.edit_button.pack(side="left")
        self.source_button = ttk.Button(actions, text="查看原发票", command=self.open_source)
        self.source_button.pack(side="left", padx=8)
        self.remove_button = ttk.Button(actions, text="移出本次清单", command=self.remove_selected)
        self.remove_button.pack(side="left")
        self.duplicate_button = ttk.Button(actions, text="重复文件", command=self.show_duplicates)
        self.duplicate_button.pack(side="left", padx=8)
        self.progress = ttk.Progressbar(frame, mode="determinate")
        self.progress.grid(row=5, column=0, sticky='ew')
        status_label = ttk.Label(frame, textvariable=self.status, wraplength=930)
        status_label.grid(row=6, column=0, sticky='w', pady=(8, 0))
        frame.bind('<Configure>', lambda event: [label.configure(wraplength=max(200, event.width - 48))
                                               for label in (self.folder_label, status_label)])
        self.set_busy(False)
        root.after(100, self.poll)

    def save_settings(self):
        values = dict(folder=self.folder.get())
        try:
            self.settings_path.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass  # Reading and exporting work even from a read-only program folder.

    def set_busy(self, busy):
        self.busy = busy
        for widget in (self.choose_button, self.reread_button, self.recursive_check, self.edit_button,
                       self.source_button, self.remove_button, self.duplicate_button):
            widget.configure(state="disabled" if busy else "normal")
        self.export_button.configure(state="disabled" if busy else "normal")

    def choose_folder(self):
        path = filedialog.askdirectory(title="选择存放 PDF 发票的文件夹", initialdir=self.folder.get() or self.last_folder or str(BASE))
        if path:
            self.folder.set(path)
            self.folder_label.grid()
            self.start_scan()

    def start_scan(self):
        if self.busy:
            return
        path = self.folder.get()
        if not Path(path).is_dir() or not path:
            messagebox.showinfo("选择文件夹", "请先选择存放 PDF 发票的文件夹。")
            return
        self.folder_label.grid()
        self.invoices, self.duplicates = [], []
        self.refresh()
        self.set_busy(True)
        self.save_settings()
        recursive = self.recursive.get()
        def work():
            try:
                result = scan_folder(path, recursive, lambda i, n, f: self.messages.put(("progress", (i, n, f))))
                self.messages.put(("done", result))
            except Exception as exc:
                self.messages.put(("error", str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        try:
            while True:
                event, value = self.messages.get_nowait()
                if event == "progress":
                    i, n, filename = value
                    self.progress.configure(maximum=n, value=i - 1)
                    self.status.set(f"正在读取 {i}/{n}：{filename}（扫描件首次识别需要稍等）")
                elif event == "done":
                    self.invoices, self.duplicates = value
                    self.progress.configure(value=self.progress["maximum"])
                    self.refresh()
                    self.set_busy(False)
                else:
                    self.set_busy(False)
                    self.status.set(value)
                    messagebox.showerror("读取失败", value)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def refresh(self):
        prepare_names(self.invoices)
        self.table.delete(*self.table.get_children())
        for i, invoice in enumerate(self.invoices):
            status = "待处理" if invoice.error else "已识别" if invoice.engine == "文本" else invoice.engine
            self.table.insert("", "end", iid=str(i), values=(status, invoice.number, invoice.name,
                              f"{invoice.total:.2f}" if invoice.total is not None else "",
                              invoice.pdf_name + '.pdf' if invoice.pdf_name else ''),
                              tags=("error",) if invoice.error else ())
        errors = sum(bool(i.error) for i in self.invoices)
        total = sum((i.total for i in self.invoices if not i.error and i.total is not None), Decimal(0))
        self.status.set(f"{len(self.invoices)} 张发票，含税合计 {total:,.2f} 元；跳过 {len(self.duplicates)} 个重复文件。"
                        + (f"还有 {errors} 项待处理，双击补填或移出清单后导出。" if errors else "可双击修改品名，确认后统一导出。"))
        self.export_button.configure(state="disabled" if self.busy else "normal")

    def selected(self):
        selection = self.table.selection()
        return int(selection[0]) if selection else None

    def edit_selected(self):
        index = self.selected()
        if self.busy or index is None:
            return
        invoice = self.invoices[index]
        dialog = tk.Toplevel(self.root)
        dialog.title("修改发票")
        dialog.transient(self.root)
        dialog.grab_set()
        body = ttk.Frame(dialog, padding=20)
        body.pack(fill="both", expand=True)
        dialog.geometry(f'{min(720, self.root.winfo_screenwidth()-80)}x{min(520, self.root.winfo_screenheight()-80)}')
        body.columnconfigure(1, weight=1)
        body.rowconfigure(5, weight=1)
        ttk.Label(body, text=invoice.path.name, wraplength=540).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
        if invoice.error:
            ttk.Label(body, text=invoice.error, foreground="#b4471f", wraplength=540).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 12))
        variables = [tk.StringVar(value=v) for v in (invoice.number, invoice.name, str(invoice.total) if invoice.total is not None else "")]
        for row, (label, variable) in enumerate(zip(("发票号码", "品名", "含税合计"), variables), 2):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=8)
            entry = ttk.Entry(body, textvariable=variable, width=1)
            entry.grid(row=row, column=1, sticky='ew', padx=(15, 0), pady=8)
            if label == '品名':
                entry.focus_set()
                entry.selection_range(0, 'end')
        if invoice.original_names:
            details = ttk.Frame(body)
            details.grid(row=5, column=0, columnspan=2, sticky='nsew', pady=12)
            details.columnconfigure(0, weight=1)
            details.rowconfigure(0, weight=1)
            raw_text = tk.Text(details, height=3, width=1, wrap='word', font=('Microsoft YaHei UI', 10))
            raw_text.insert('1.0', '原始项目与规格：\n' + invoice.original_names)
            raw_text.configure(state='disabled')
            raw_text.grid(row=0, column=0, sticky='nsew')
            scrollbar = ttk.Scrollbar(details, command=raw_text.yview)
            scrollbar.grid(row=0, column=1, sticky='ns')
            raw_text.configure(yscrollcommand=scrollbar.set)
        def save():
            number, name, raw_total = (v.get().strip() for v in variables)
            if not re.fullmatch(r"\d{8,24}", number) or not name:
                messagebox.showerror("内容不完整", "发票号码应为 8 至 24 位数字，品名不能为空。", parent=dialog)
                return
            try:
                total = money(raw_total)
                name = pdf_stem(name)
            except ValueError as exc:
                messagebox.showerror("内容不正确", str(exc), parent=dialog)
                return
            invoice.number, invoice.name, invoice.total = number, name, total
            invoice.error, invoice.engine = "", "已修改"
            self.refresh()
            dialog.destroy()
        ttk.Button(body, text="确认品名", style="Accent.TButton", command=save).grid(row=6, column=1, sticky="e", pady=(14, 0))

    def open_source(self):
        index = self.selected()
        if index is not None:
            try:
                os.startfile(self.invoices[index].path)
            except OSError as exc:
                messagebox.showerror("无法打开", str(exc))

    def remove_selected(self):
        for index in sorted((int(x) for x in self.table.selection()), reverse=True):
            del self.invoices[index]
        self.refresh()

    def show_duplicates(self):
        if not self.duplicates:
            messagebox.showinfo("重复文件", "没有发现重复文件。")
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("已跳过的重复发票")
        text = tk.Text(dialog, width=95, height=20, wrap="word", font=("Microsoft YaHei UI", 10))
        text.pack(fill="both", expand=True, padx=15, pady=15)
        for duplicate, first in self.duplicates:
            text.insert("end", f"跳过：{duplicate}\n保留：{first}\n\n")
        text.configure(state="disabled")

    def export(self):
        if not self.invoices:
            messagebox.showinfo('尚未读取发票', '请先选择发票文件夹并读取。')
            return
        errors = [i for i in self.invoices if i.error]
        if errors:
            messagebox.showinfo("还有待处理文件", "请双击待处理行补填内容，或选中后移出本次清单。")
            return
        template = default_template()
        if not template.is_file():
            messagebox.showerror("模板缺失", "请把报销清单表.xlsx 放到程序旁边。")
            return
        filename = filedialog.asksaveasfilename(title="保存报销清单", initialdir=self.folder.get(),
                    initialfile=f"报销清单_{datetime.now():%Y%m%d_%H%M%S}.xlsx", defaultextension=".xlsx", filetypes=[("Excel 工作簿", "*.xlsx")])
        if not filename:
            return
        try:
            self.duplicates, renamed = export_bundle(template, filename, self.invoices, self.duplicates)
        except PermissionError:
            self.refresh()
            messagebox.showerror("无法保存", "请关闭正在打开的同名表格或发票 PDF 后重试，也可更换保存位置。")
            return
        except Exception as exc:
            self.refresh()
            messagebox.showerror("导出失败", str(exc))
            return
        self.save_settings()
        self.last_output = filename
        self.refresh()
        total = sum((i.total for i in self.invoices), Decimal(0))
        self.status.set(f"已导出 {len(self.invoices)} 张发票，含税合计 {total:,.2f} 元；已重命名 {len(renamed)} 个 PDF。")
        detail = f"已保存 {len(self.invoices)} 张发票，合计 {total:,.2f} 元。\n已重命名 {len(renamed)} 个 PDF。\n\n{filename}"
        messagebox.showinfo("导出完成", detail)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--folder")
    parser.add_argument("--output")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.folder and args.output:
        invoices, duplicates = scan_folder(args.folder)
        duplicates, renamed = export_bundle(default_template(), args.output, invoices, duplicates)
        result = {"invoices": len(invoices), "duplicates": len(duplicates), "total": str(sum(i.total for i in invoices)),
                  "renamed": len(renamed), "rename_errors": []}
        Path(args.output).with_suffix(".result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        return
    if sys.platform == "win32":
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except OSError:
            pass
    root = tk.Tk()
    if args.self_test:
        root.withdraw()
    app = App(root)
    if args.self_test:
        root.update()
        root.destroy()
        return
    root.mainloop()


if __name__ == "__main__":
    main()
