import tempfile
import time
import tkinter as tk
import unittest
import shutil
import json
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile
import xml.etree.ElementTree as ET
from template_export import N
from decimal import Decimal

from invoice_app import App, UnifiedApp
from invoice_core import Invoice, PROBLEM_INVOICE_FOLDER, archive_problem_pdfs

ROOT = Path(__file__).resolve().parents[1]
BUILD_DIR = ROOT / '.build'
BUILD_DIR.mkdir(exist_ok=True)


class DesktopWorkflow(unittest.TestCase):
    def test_unified_window_switches_between_two_modes(self):
        root = tk.Tk()
        root.withdraw()
        app = UnifiedApp(root)
        try:
            root.update_idletasks()
            self.assertEqual(app.mode.get(), 'reimbursement')
            self.assertEqual(app.pages['reimbursement'].winfo_manager(), 'pack')
            self.assertEqual(app.pages['sorting'].winfo_manager(), '')
            app.mode.set('sorting')
            app.show_mode()
            self.assertEqual(app.pages['reimbursement'].winfo_manager(), '')
            self.assertEqual(app.pages['sorting'].winfo_manager(), 'pack')
            self.assertIn('按金额整理和排列', root.title())
        finally:
            app.close()

    def test_read_preview_export(self):
        fixture_folder = ROOT / '测试用发票'
        if not fixture_folder.is_dir():
            self.skipTest('测试用发票样本不存在')
        root = tk.Tk()
        root.withdraw()
        try:
            with patch('invoice_app.Path.read_text', return_value='{"recursive":true,"folder":"old folder"}'):
                app = App(root)
            self.assertFalse(app.recursive.get())
            self.assertEqual(app.folder.get(), '')
            self.assertFalse(app.folder_label.winfo_manager())
            with tempfile.TemporaryDirectory(dir=ROOT / '.build') as temp:
                app.settings_path = Path(temp) / 'settings.json'
                source = Path(temp) / 'invoices'
                source.mkdir()
                for pdf in fixture_folder.rglob('*.pdf'):
                    destination = source / pdf.relative_to(fixture_folder)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(pdf, destination)
                app.folder.set(str(source))
                app.recursive.set(True)
                self.assertFalse(hasattr(app, 'person'))
                self.assertFalse(hasattr(app, 'student'))
                app.start_scan()
                self.assertEqual(app.folder_label.winfo_manager(), 'grid')
                deadline = time.monotonic() + 15
                while app.busy and time.monotonic() < deadline:
                    root.update()
                    time.sleep(.02)
                self.assertFalse(app.busy)
                self.assertEqual(len(app.table.get_children()), 29)
                self.assertIn('5,191.19', app.status.get())
                self.assertEqual(str(app.export_button['state']), 'normal')
                # Editing the dialog must not rename or export before final confirmation.
                invoice = next(item for item in app.invoices if item.risk == 'normal')
                invoice_index = app.invoices.index(invoice)
                red_invoice = next(item for item in app.invoices if item.risk == 'red')
                red_original = red_invoice.path
                original = invoice.path
                app.table.selection_set(str(invoice_index))
                app.edit_selected()
                dialog = next(w for w in root.winfo_children() if isinstance(w, tk.Toplevel))
                body = dialog.winfo_children()[0]
                name_entry = next(w for w in body.winfo_children() if w.winfo_class() == 'TEntry' and int(w.grid_info()['row']) == 3)
                name_entry.delete(0, 'end')
                name_entry.insert(0, '自定义/品名:*')
                save = next(w for w in body.winfo_children() if w.winfo_class() == 'TButton')
                save.invoke()
                self.assertEqual(invoice.name, '自定义／品名：×')
                edited_index = app.invoices.index(invoice)
                self.assertEqual(app.table.item(str(edited_index), 'values')[4], invoice.pdf_name + '.pdf')
                self.assertTrue(original.exists())
                with patch('invoice_app.messagebox.askokcancel', return_value=True), \
                     patch('invoice_app.filedialog.asksaveasfilename', return_value=''):
                    app.export()
                self.assertTrue(original.exists())
                output = Path(temp) / 'gui.xlsx'
                with patch('invoice_app.filedialog.asksaveasfilename', return_value=str(output)), \
                     patch('invoice_app.messagebox.askokcancel', return_value=True), \
                     patch('invoice_app.messagebox.showinfo') as done:
                    app.export()
                    done.assert_called_once()
                self.assertTrue(output.is_file())
                self.assertEqual(invoice.path.name, invoice.name + '.pdf')
                self.assertFalse(original.exists())
                self.assertEqual(red_invoice.path, red_original)
                self.assertTrue(red_original.exists())
                with ZipFile(output) as z:
                    sheet = ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
                    names = [cell.findtext('s:is/s:t', namespaces=N) for cell in
                             sheet.findall(".//s:c[@t='inlineStr']", N)]
                    self.assertIn(invoice.name, names)
                self.assertIn('已导出 24', app.status.get())
                self.assertFalse({'person', 'student'} & json.loads(app.settings_path.read_text(encoding='utf-8')).keys())
                self.assertTrue(all(i.path.exists() for i in app.invoices))
                self.assertTrue(all(not p.name.startswith('2026') for p in source.rglob('*.pdf')))
        finally:
            app.close()

    def test_confirmation_buttons_fit_at_high_scaling(self):
        root = tk.Tk()
        root.withdraw()
        try:
            root.tk.call('tk', 'scaling', 2.0)
            app = App(root)
            root.geometry('850x560+20000+20000')
            root.deiconify()
            root.update()
            def inside(widget, window):
                self.assertTrue(widget.winfo_viewable())
                self.assertGreaterEqual(widget.winfo_rootx(), window.winfo_rootx())
                self.assertGreaterEqual(widget.winfo_rooty(), window.winfo_rooty())
                self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), window.winfo_rootx() + window.winfo_width())
                self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), window.winfo_rooty() + window.winfo_height())
            inside(app.export_button, root)
            self.assertEqual(app.export_button['text'], '确认并导出')
            self.assertEqual(str(app.export_button['state']), 'normal')
            app.invoices = [Invoice(ROOT / '.build/test.pdf', '00000001', '测试', Decimal(1), original_names='长规格\n' * 40)]
            app.refresh()
            app.table.selection_set('0')
            app.edit_selected()
            dialog = next(w for w in root.winfo_children() if isinstance(w, tk.Toplevel))
            dialog.geometry('720x520+20000+20000')
            root.update()
            confirm = next(w for w in dialog.winfo_children()[0].winfo_children() if w.winfo_class() == 'TButton')
            self.assertEqual(confirm['text'], '确认品名')
            inside(confirm, dialog)
            dialog.destroy()
        finally:
            app.close()

    def test_red_rows_are_sorted_styled_and_excluded_from_export(self):
        root = tk.Tk()
        root.withdraw()
        try:
            app = App(root)
            with tempfile.TemporaryDirectory(dir=ROOT / '.build') as temp:
                folder = Path(temp)
                red_path, normal_path = folder / 'red.pdf', folder / 'normal.pdf'
                red_path.write_bytes(b'red')
                normal_path.write_bytes(b'normal')
                red = Invoice(red_path, '00000001', '测试材料', Decimal('20'), units=('批',))
                normal = Invoice(normal_path, '00000002', '普通材料', Decimal('30'))
                app.folder.set(str(folder))
                app.invoices = [normal, red]
                app.refresh()
                self.assertEqual([invoice.risk for invoice in app.invoices], ['red', 'normal'])
                self.assertEqual(app.table.item('0', 'tags'), ('red',))
                self.assertIn('单位为批', app.table.item('0', 'values')[0])
                output = folder / 'out.xlsx'
                with patch('invoice_app.messagebox.askokcancel', return_value=True), \
                     patch('invoice_app.filedialog.asksaveasfilename', return_value=str(output)), \
                     patch('invoice_app.messagebox.showinfo'):
                    app.export()
                self.assertTrue(red_path.exists())
                self.assertFalse(normal_path.exists())
                self.assertTrue((folder / '普通材料.pdf').exists())
                with ZipFile(output) as workbook:
                    sheet = ET.fromstring(workbook.read('xl/worksheets/sheet1.xml'))
                    rows = sheet.findall('s:sheetData/s:row', N)
                    self.assertEqual(len(rows), 2)
                    self.assertEqual(rows[1].find("s:c[@r='C2']/s:is/s:t", N).text, '00000002')
        finally:
            app.close()

    def test_one_click_red_removal_archives_files_and_explains_recovery(self):
        root = tk.Tk()
        root.withdraw()
        try:
            app = App(root)
            with tempfile.TemporaryDirectory(dir=ROOT / '.build') as temp:
                folder = Path(temp)
                red_path, normal_path = folder / 'red.pdf', folder / 'normal.pdf'
                red_path.write_bytes(b'red invoice')
                normal_path.write_bytes(b'normal invoice')
                app.folder.set(str(folder))
                app.invoices = [
                    Invoice(normal_path, '00000002', '普通材料', Decimal('30')),
                    Invoice(red_path, '00000001', '测试材料', Decimal('20'), units=('批',)),
                ]
                app.refresh()

                def archive_without_real_recycle(paths, selected_folder):
                    return archive_problem_pdfs(
                        paths, selected_folder, recycler=lambda originals: [path.unlink() for path in originals])

                with patch('invoice_app.archive_problem_pdfs', side_effect=archive_without_real_recycle), \
                     patch('invoice_app.messagebox.showinfo') as notice:
                    app.remove_all_red()
                    deadline = time.monotonic() + 3
                    while app.busy and time.monotonic() < deadline:
                        root.update()
                        time.sleep(.01)
                    root.update()

                self.assertFalse(app.busy)
                self.assertEqual([invoice.path for invoice in app.invoices], [normal_path])
                self.assertFalse(red_path.exists())
                self.assertEqual((folder / PROBLEM_INVOICE_FOLDER / 'red.pdf').read_bytes(), b'red invoice')
                notice.assert_called_once()
                self.assertIn('回收站', notice.call_args.args[1])
                self.assertIn('还原', notice.call_args.args[1])
        finally:
            app.close()

    def test_dropped_external_pdf_targets_selected_read_folder(self):
        root = tk.Tk()
        root.withdraw()
        try:
            app = App(root)
            with tempfile.TemporaryDirectory(dir=ROOT / '.build') as temp:
                base = Path(temp)
                read_folder, outside = base / 'read', base / 'outside'
                read_folder.mkdir()
                outside.mkdir()
                source = outside / 'invoice.pdf'
                source.write_bytes(b'pdf')
                invoice = Invoice(source, '00000001', '测试材料', Decimal('1'))
                app.folder.set(str(read_folder))
                with patch('invoice_app.scan_files', return_value=([invoice], [])):
                    app.handle_drop_paths([str(source)])
                    deadline = time.monotonic() + 3
                    while app.busy and time.monotonic() < deadline:
                        root.update()
                        time.sleep(.01)
                self.assertFalse(app.busy)
                self.assertEqual(len(app.invoices), 1)
                self.assertEqual(app.invoices[0].destination_folder, read_folder.resolve())
                self.assertEqual(app.invoices[0].pdf_name, '测试材料')
                self.assertTrue(source.exists())
        finally:
            app.close()


if __name__ == '__main__':
    unittest.main()
