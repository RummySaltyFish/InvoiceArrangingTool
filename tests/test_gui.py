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

from invoice_app import App

ROOT = Path(__file__).resolve().parents[1]


class DesktopWorkflow(unittest.TestCase):
    def test_read_preview_export(self):
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
                for pdf in (ROOT / 'baoxiao/uploads').glob('*.pdf'):
                    shutil.copy2(pdf, source / pdf.name)
                app.folder.set(str(source))
                self.assertFalse(hasattr(app, 'person'))
                self.assertFalse(hasattr(app, 'student'))
                app.start_scan()
                self.assertEqual(app.folder_label.winfo_manager(), 'grid')
                deadline = time.monotonic() + 15
                while app.busy and time.monotonic() < deadline:
                    root.update()
                    time.sleep(.02)
                self.assertFalse(app.busy)
                self.assertEqual(len(app.table.get_children()), 16)
                self.assertEqual(len(app.duplicates), 3)
                self.assertIn('1,442.24', app.status.get())
                self.assertEqual(str(app.export_button['state']), 'normal')
                # Editing the dialog must not rename or export before final confirmation.
                invoice = app.invoices[0]
                original = invoice.path
                app.table.selection_set('0')
                app.edit_selected()
                dialog = next(w for w in root.winfo_children() if isinstance(w, tk.Toplevel))
                body = dialog.winfo_children()[0]
                name_entry = next(w for w in body.winfo_children() if w.winfo_class() == 'TEntry' and int(w.grid_info()['row']) == 3)
                name_entry.delete(0, 'end')
                name_entry.insert(0, '自定义/品名:*')
                save = next(w for w in body.winfo_children() if w.winfo_class() == 'TButton')
                save.invoke()
                self.assertEqual(invoice.name, '自定义／品名：×')
                self.assertEqual(app.table.item('0', 'values')[4], invoice.pdf_name + '.pdf')
                self.assertTrue(original.exists())
                with patch('invoice_app.filedialog.asksaveasfilename', return_value=''):
                    app.export()
                self.assertTrue(original.exists())
                output = Path(temp) / 'gui.xlsx'
                with patch('invoice_app.filedialog.asksaveasfilename', return_value=str(output)), \
                     patch('invoice_app.messagebox.showinfo') as done:
                    app.export()
                    done.assert_called_once()
                self.assertTrue(output.is_file())
                self.assertEqual(invoice.path.name, invoice.name + '.pdf')
                self.assertFalse(original.exists())
                with ZipFile(output) as z:
                    sheet = ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
                    self.assertEqual(sheet.find(".//s:c[@r='D2']/s:is/s:t", N).text, invoice.path.stem)
                self.assertIn('已导出 16', app.status.get())
                self.assertFalse({'person', 'student'} & json.loads(app.settings_path.read_text(encoding='utf-8')).keys())
                self.assertTrue(all(i.path.exists() for i in app.invoices))
                self.assertTrue(all(not p.name.startswith('2026') for p in source.glob('*.pdf')))
        finally:
            root.destroy()

    def test_confirmation_buttons_fit_at_high_scaling(self):
        from invoice_core import Invoice
        from decimal import Decimal
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
            root.destroy()


if __name__ == '__main__':
    unittest.main()
