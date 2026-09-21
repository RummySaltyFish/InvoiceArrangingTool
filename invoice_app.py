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

from invoice_core import (archive_problem_pdfs, deduplicate_invoices, evaluate_invoices,
                          money, pdf_stem, prepare_names, scan_files, scan_folder)
from invoice_sorter import App as SorterApp
from template_export import export_bundle

BASE = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
RESOURCES = Path(getattr(sys, "_MEIPASS", BASE))


def default_template():
    external = BASE / "报销清单表.xlsx"
    return external if external.is_file() else RESOURCES / "报销清单表.xlsx"


def enable_windows_file_drop(root, callback):
    """Accept PDF paths dropped from Windows Explorer without an extra package."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    hwnd = root.winfo_id()
    shell32, user32 = ctypes.windll.shell32, ctypes.windll.user32
    shell32.DragAcceptFiles.argtypes = (wintypes.HWND, wintypes.BOOL)
    shell32.DragQueryFileW.argtypes = (wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT)
    shell32.DragQueryFileW.restype = wintypes.UINT
    shell32.DragFinish.argtypes = (wintypes.HANDLE,)
    get_proc = user32.GetWindowLongPtrW
    set_proc = user32.SetWindowLongPtrW
    get_proc.argtypes = (wintypes.HWND, ctypes.c_int)
    get_proc.restype = ctypes.c_void_p
    set_proc.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_void_p)
    set_proc.restype = ctypes.c_void_p
    call_proc = user32.CallWindowProcW
    call_proc.argtypes = (ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    call_proc.restype = ctypes.c_ssize_t
    old_proc = get_proc(hwnd, -4)
    procedure_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                       wintypes.WPARAM, wintypes.LPARAM)

    @procedure_type
    def procedure(window, message, wparam, lparam):
        if message == 0x0233:  # WM_DROPFILES
            paths = []
            try:
                count = shell32.DragQueryFileW(wparam, 0xFFFFFFFF, None, 0)
                for index in range(count):
                    length = shell32.DragQueryFileW(wparam, index, None, 0)
                    buffer = ctypes.create_unicode_buffer(length + 1)
                    shell32.DragQueryFileW(wparam, index, buffer, length + 1)
                    paths.append(buffer.value)
            finally:
                shell32.DragFinish(wparam)
            root.after(0, callback, paths)
            return 0
        return call_proc(old_proc, window, message, wparam, lparam)

    shell32.DragAcceptFiles(hwnd, True)
    set_proc(hwnd, -4, ctypes.cast(procedure, ctypes.c_void_p))
    root._file_drop_procedure = procedure
    root._file_drop_old_procedure = old_proc
    root._file_drop_hwnd = hwnd
    root._file_drop_set_procedure = set_proc
    root._file_drop_shell = shell32
    return True


def disable_windows_file_drop(root):
    old_proc = getattr(root, "_file_drop_old_procedure", None)
    if not old_proc:
        return
    import ctypes
    root._file_drop_shell.DragAcceptFiles(root._file_drop_hwnd, False)
    root._file_drop_set_procedure(root._file_drop_hwnd, -4, ctypes.c_void_p(old_proc))
    root._file_drop_old_procedure = None


class App:
    def __init__(self, root, parent=None, standalone=True, manage_drop=True):
        self.root = root
        self.standalone = standalone
        self.manage_drop = manage_drop
        if standalone:
            root.protocol("WM_DELETE_WINDOW", self.close)
        self.invoices = []
        self.duplicates = []
        self.messages = queue.Queue()
        self.busy = False
        self.last_output = None
        self.closed = False
        self.poll_job = None
        self.settings_path = BASE / "报销工具设置.json"
        try:
            settings = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            settings = {}
        self.last_folder = settings.get('folder', '')
        self.folder = tk.StringVar(value='')
        self.recursive = tk.BooleanVar(value=False)
        self.expense_type = tk.StringVar(value="材料")
        self.status = tk.StringVar(value="选择发票文件夹后开始读取。")
        if standalone:
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
        frame = ttk.Frame(parent or root, padding=24)
        frame.pack(fill="both", expand=True)
        self.frame = frame
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
        ttk.Label(choices, text="报销经费：").pack(side="left", padx=(10, 2))
        self.expense_type_box = ttk.Combobox(choices, textvariable=self.expense_type,
                                             values=("材料", "办公", "设备"), width=6, state="readonly")
        self.expense_type_box.pack(side="left")
        self.expense_type_box.bind("<<ComboboxSelected>>", lambda _: self.refresh())
        self.export_button = ttk.Button(top, text="确认并导出", style="Accent.TButton", command=self.export)
        self.export_button.grid(row=0, column=1, sticky='e', padx=(12, 0))
        self.folder_label = ttk.Label(frame, textvariable=self.folder, style='Hint.TLabel', wraplength=920)
        self.folder_label.grid(row=2, column=0, sticky='w', pady=(0, 12))
        self.folder_label.grid_remove()
        table_frame = ttk.Frame(frame)
        table_frame.grid(row=3, column=0, sticky='nsew')
        self.table = ttk.Treeview(table_frame, columns=("status", "number", "name", "total", "file"), show="headings", selectmode="extended")
        for key, title, width in (("status", "状态 / 问题", 190), ("number", "发票号码", 200), ("name", "品名", 220),
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
        self.table.tag_configure("red", foreground="white", background="#c62828")
        self.table.tag_configure("blue", foreground="white", background="#1565c0")
        self.table.tag_configure("yellow", foreground="#1f2937", background="#ffe082")
        self.table.tag_configure("error", foreground="#9a3412", background="#ffedd5")
        self.table.bind("<Double-1>", lambda _: self.edit_selected())
        self.table.bind("<Button-3>", self.show_context_menu)
        self.context_menu = tk.Menu(root, tearoff=False)
        actions = ttk.Frame(frame)
        actions.grid(row=4, column=0, sticky='ew', pady=12)
        self.edit_button = ttk.Button(actions, text="修改品名", command=self.edit_selected)
        self.edit_button.pack(side="left")
        self.source_button = ttk.Button(actions, text="查看原发票", command=self.open_source)
        self.source_button.pack(side="left", padx=8)
        self.remove_button = ttk.Button(actions, text="移出本次清单", command=self.remove_selected)
        self.remove_button.pack(side="left")
        self.remove_red_button = ttk.Button(actions, text="一键移除标红", command=self.remove_all_red)
        self.remove_red_button.pack(side="left", padx=8)
        self.duplicate_button = ttk.Button(actions, text="重复文件", command=self.show_duplicates)
        self.duplicate_button.pack(side="left")
        self.progress = ttk.Progressbar(frame, mode="determinate")
        self.progress.grid(row=5, column=0, sticky='ew')
        status_label = ttk.Label(frame, textvariable=self.status, wraplength=930)
        status_label.grid(row=6, column=0, sticky='w', pady=(8, 0))
        frame.bind('<Configure>', lambda event: [label.configure(wraplength=max(200, event.width - 48))
                                               for label in (self.folder_label, status_label)])
        self.set_busy(False)
        self.poll_job = root.after(100, self.poll)
        if manage_drop:
            root.after_idle(lambda: enable_windows_file_drop(root, self.handle_drop_paths))

    def shutdown(self):
        self.closed = True
        if self.poll_job is not None:
            try:
                self.root.after_cancel(self.poll_job)
            except tk.TclError:
                pass
            self.poll_job = None
        if self.manage_drop:
            disable_windows_file_drop(self.root)

    def close(self):
        self.shutdown()
        if self.standalone:
            self.root.destroy()

    def save_settings(self):
        values = dict(folder=self.folder.get())
        try:
            self.settings_path.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass  # Reading and exporting work even from a read-only program folder.

    def set_busy(self, busy):
        self.busy = busy
        for widget in (self.choose_button, self.reread_button, self.recursive_check, self.edit_button,
                       self.expense_type_box, self.source_button, self.remove_button,
                       self.remove_red_button, self.duplicate_button):
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

    def handle_drop_paths(self, raw_paths):
        if self.busy:
            return
        paths = []
        existing = {invoice.path.resolve() for invoice in self.invoices}
        for raw in raw_paths:
            path = Path(raw)
            if path.is_file() and path.suffix.lower() == ".pdf" and path.resolve() not in existing:
                resolved = path.resolve()
                if resolved not in paths:
                    paths.append(resolved)
        if not paths:
            self.status.set("拖入内容中没有新的 PDF 文件。")
            return
        folder = Path(self.folder.get()) if self.folder.get() else paths[0].parent
        if not folder.is_dir():
            folder = paths[0].parent
        destination = folder.resolve()
        self.folder.set(str(destination))
        self.folder_label.grid()
        self.save_settings()
        self.set_busy(True)

        def work():
            try:
                invoices, duplicates = scan_files(
                    paths, lambda i, n, f: self.messages.put(("progress", (i, n, f))))
                self.messages.put(("drop_done", (invoices, duplicates, destination)))
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
                elif event == "drop_done":
                    new_invoices, new_duplicates, destination = value
                    for invoice in new_invoices:
                        if invoice.path.parent.resolve() != destination:
                            invoice.destination_folder = destination
                    self.invoices, cross_duplicates = deduplicate_invoices(self.invoices + new_invoices)
                    self.duplicates.extend(new_duplicates)
                    self.duplicates.extend(cross_duplicates)
                    self.progress.configure(value=self.progress["maximum"])
                    self.refresh()
                    self.set_busy(False)
                elif event == "red_archive_done":
                    self.set_busy(False)
                    result = value
                    moved_lookup = {str(source).casefold(): target for source, target in result.moved}
                    archived = set(moved_lookup)
                    archived.update(str(path).casefold() for path in result.already_present)
                    self.invoices = [invoice for invoice in self.invoices
                                     if str(invoice.path.resolve()).casefold() not in archived]
                    self.duplicates = [
                        (moved_lookup.get(str(duplicate.resolve()).casefold(), duplicate),
                         moved_lookup.get(str(first.resolve()).casefold(), first))
                        for duplicate, first in self.duplicates
                    ]
                    self.refresh()
                    moved_count = len(result.moved)
                    already_count = len(result.already_present)
                    failure_count = len(result.failures)
                    archived_count = moved_count + already_count
                    self.status.set(f"已移出 {archived_count} 张标红发票；{failure_count} 张未移动。")
                    detail = []
                    if moved_count:
                        detail.append(f"已将 {moved_count} 张标红发票复制并校验到：\n{result.folder}")
                        detail.append("原文件已送入 Windows 回收站。需要恢复时，请打开回收站，选中文件后点击“还原”；问题文件夹中的副本不会自动删除。")
                    if already_count:
                        detail.append(f"另有 {already_count} 张发票原本就在该问题文件夹中，已从当前列表移除。")
                    if result.failures:
                        errors = "\n".join(f"{path.name}：{reason}" for path, reason in result.failures[:8])
                        if len(result.failures) > 8:
                            errors += f"\n另有 {len(result.failures) - 8} 张未列出。"
                        detail.append("以下原文件仍保留在原位置：\n" + errors)
                        messagebox.showwarning("部分标红发票未移动", "\n\n".join(detail))
                    else:
                        messagebox.showinfo("标红发票已移出", "\n\n".join(detail))
                elif event == "red_archive_error":
                    self.set_busy(False)
                    self.status.set("标红发票移动失败，原列表保持不变。")
                    messagebox.showerror("无法移动标红发票", value)
                else:
                    self.set_busy(False)
                    self.status.set(value)
                    messagebox.showerror("读取失败", value)
        except queue.Empty:
            pass
        if not self.closed:
            self.poll_job = self.root.after(100, self.poll)

    def refresh(self):
        self.invoices = evaluate_invoices(self.invoices, self.expense_type.get())
        prepare_names(self.invoices)
        self.table.delete(*self.table.get_children())
        for i, invoice in enumerate(self.invoices):
            if invoice.risk_note:
                status = invoice.risk_note
            elif invoice.red_override and invoice.red_reasons:
                status = "已取消标红"
            else:
                status = "待处理" if invoice.error else "已识别" if invoice.engine == "文本" else invoice.engine
            tag = invoice.risk if invoice.risk != "normal" else "error" if invoice.error else ""
            self.table.insert("", "end", iid=str(i), values=(status, invoice.number, invoice.name,
                              f"{invoice.total:.2f}" if invoice.total is not None else "",
                              invoice.pdf_name + '.pdf' if invoice.pdf_name else ''),
                              tags=(tag,) if tag else ())
        errors = sum(bool(i.error) for i in self.invoices)
        red = sum(i.risk == "red" for i in self.invoices)
        blue = sum(i.risk == "blue" for i in self.invoices)
        yellow = sum(i.risk == "yellow" for i in self.invoices)
        total = sum((i.total for i in self.invoices if not i.error and i.total is not None and i.risk != "red"), Decimal(0))
        self.status.set(f"{len(self.invoices)} 张发票，含税合计 {total:,.2f} 元；跳过 {len(self.duplicates)} 个重复文件。"
                        + f"标红 {red}、蓝色 {blue}、黄色 {yellow}。"
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

    def remove_all_red(self):
        self.invoices = evaluate_invoices(self.invoices, self.expense_type.get())
        red = [invoice for invoice in self.invoices if invoice.risk == "red"]
        if not red:
            messagebox.showinfo("一键移除标红", "当前没有标红发票。")
            return
        selected_folder = Path(self.folder.get())
        if not selected_folder.is_dir():
            messagebox.showerror("无法移动标红发票", "读取文件夹不存在，请重新选择文件夹。")
            return
        paths = tuple(invoice.path for invoice in red)
        self.set_busy(True)
        self.status.set(f"正在复制并校验 {len(paths)} 张标红发票，请稍候……")

        def work():
            try:
                self.messages.put(("red_archive_done", archive_problem_pdfs(paths, selected_folder)))
            except Exception as exc:
                self.messages.put(("red_archive_error", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def show_context_menu(self, event):
        row = self.table.identify_row(event.y)
        if not row:
            return
        self.table.selection_set(row)
        invoice = self.invoices[int(row)]
        self.context_menu.delete(0, "end")
        if invoice.risk == "red":
            self.context_menu.add_command(label="取消标红", command=self.cancel_selected_red)
        elif invoice.red_override and invoice.red_reasons:
            self.context_menu.add_command(label="恢复自动标红", command=self.restore_selected_red)
        else:
            return
        self.context_menu.tk_popup(event.x_root, event.y_root)

    def cancel_selected_red(self):
        index = self.selected()
        if index is not None:
            self.invoices[index].red_override = True
            self.refresh()

    def restore_selected_red(self):
        index = self.selected()
        if index is not None:
            self.invoices[index].red_override = False
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
        self.invoices = evaluate_invoices(self.invoices, self.expense_type.get())
        red = [invoice for invoice in self.invoices if invoice.risk == "red"]
        exportable = [invoice for invoice in self.invoices if invoice.risk != "red"]
        if red and not messagebox.askokcancel(
                "标红发票不会导出",
                f"有 {len(red)} 张标红发票无法报销，不会写入 Excel，也不会复制或重命名 PDF。\n\n继续导出其余发票吗？"):
            return
        if not exportable:
            messagebox.showinfo("没有可导出的发票", "所有发票均已标红，本次没有可导出的内容。")
            return
        errors = [i for i in exportable if i.error]
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
            self.duplicates, operations = export_bundle(template, filename, exportable, self.duplicates)
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
        total = sum((i.total for i in exportable), Decimal(0))
        renamed = sum(action == "rename" for _, _, action in operations)
        copied = sum(action == "copy" for _, _, action in operations)
        self.status.set(f"已导出 {len(exportable)} 张发票，含税合计 {total:,.2f} 元；"
                        f"已重命名 {renamed} 个、复制 {copied} 个 PDF。")
        detail = (f"已保存 {len(exportable)} 张发票，合计 {total:,.2f} 元。\n"
                  f"已重命名 {renamed} 个、复制 {copied} 个 PDF。"
                  + (f"\n已排除 {len(red)} 张标红发票。" if red else "") + f"\n\n{filename}")
        messagebox.showinfo("导出完成", detail)


class UnifiedApp:
    """One window with two independent invoice workflows."""

    def __init__(self, root):
        self.root = root
        self.closed = False
        root.title("发票工具")
        root.geometry("1080x720")
        root.minsize(900, 600)
        root.configure(bg="#f4f6f8")
        root.protocol("WM_DELETE_WINDOW", self.close)

        shell = ttk.Frame(root)
        shell.pack(fill="both", expand=True)
        switch = ttk.Frame(shell, padding=(24, 14, 24, 8))
        switch.pack(fill="x")
        ttk.Label(switch, text="工作模式：", font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        self.mode = tk.StringVar(value="reimbursement")
        ttk.Radiobutton(switch, text="报销清单", value="reimbursement", variable=self.mode,
                        command=self.show_mode).pack(side="left", padx=(6, 4))
        ttk.Radiobutton(switch, text="按金额整理和排列", value="sorting", variable=self.mode,
                        command=self.show_mode).pack(side="left", padx=4)

        content = ttk.Frame(shell)
        content.pack(fill="both", expand=True)
        self.pages = {
            "reimbursement": ttk.Frame(content),
            "sorting": ttk.Frame(content),
        }
        self.reimbursement = App(root, parent=self.pages["reimbursement"],
                                 standalone=False, manage_drop=False)
        self.sorter = SorterApp(root, parent=self.pages["sorting"], standalone=False)
        self.show_mode()
        root.after_idle(lambda: enable_windows_file_drop(root, self.handle_drop_paths))

    def show_mode(self):
        for page in self.pages.values():
            page.pack_forget()
        mode = self.mode.get()
        self.pages[mode].pack(fill="both", expand=True)
        self.root.title("发票工具 - " + ("报销清单" if mode == "reimbursement" else "按金额整理和排列"))

    def handle_drop_paths(self, paths):
        if self.mode.get() == "reimbursement":
            self.reimbursement.handle_drop_paths(paths)
        else:
            self.sorter.handle_drop_paths(paths)

    def close(self):
        if self.closed:
            return
        self.closed = True
        disable_windows_file_drop(self.root)
        self.reimbursement.shutdown()
        self.sorter.shutdown()
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--folder")
    parser.add_argument("--output")
    parser.add_argument("--type", choices=("材料", "办公", "设备"), default="材料")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.folder and args.output:
        invoices, duplicates = scan_folder(args.folder)
        invoices = evaluate_invoices(invoices, args.type)
        excluded = [invoice for invoice in invoices if invoice.risk == "red"]
        exportable = [invoice for invoice in invoices if invoice.risk != "red"]
        duplicates, operations = export_bundle(default_template(), args.output, exportable, duplicates)
        result = {"invoices": len(exportable), "excluded": len(excluded), "duplicates": len(duplicates),
                  "total": str(sum(i.total for i in exportable)),
                  "renamed": sum(action == "rename" for _, _, action in operations),
                  "copied": sum(action == "copy" for _, _, action in operations), "rename_errors": []}
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
    app = UnifiedApp(root)
    if args.self_test:
        root.update()
        app.close()
        return
    root.mainloop()


if __name__ == "__main__":
    main()
