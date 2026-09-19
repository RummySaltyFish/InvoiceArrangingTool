import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile
import xml.etree.ElementTree as ET

from invoice_core import Invoice, clean_name, summarize_names, money, scan_folder, parse_pages, rename_pdfs, clean_spec, summarize_items, items_from_page, prepare_names, pdf_stem, collision_name
from template_export import export_workbook, export_bundle, N

ROOT = Path(__file__).resolve().parents[1]


class Names(unittest.TestCase):
    def test_prefix_and_wrap(self):
        self.assertEqual(clean_name('*金属制品*304圆柱头螺\n钉'), '螺钉')
        self.assertEqual(clean_name('*橡胶制品*铂金硅胶\nECOFLEX 00-30'), '铂金硅胶ECOFLEX 00-30')
        self.assertEqual(clean_name('*类别一**类别二*丁腈\n手套'), '丁腈手套')

    def test_simplify_without_merging_unrelated_goods(self):
        self.assertEqual(summarize_names(['*金属*螺栓M4', '*金属*螺母M4', '*金属*螺栓M8']), '螺栓、螺母')
        self.assertEqual(summarize_names(['*电子*开发板', '*电子*价外费用', '*电子*开发板']), '开发板')
        self.assertEqual(clean_name('*电子*价外费用'), '')
        self.assertEqual(clean_name('*电子*模块【赠品】'), '模块')
        self.assertEqual(clean_name('*工具*十字螺丝刀'), '十字螺丝刀')
        self.assertEqual(clean_name('*塑料*塑料烧杯塑料烧杯'), '塑料烧杯')

    def test_money(self):
        self.assertEqual(money('￥1,442.24'), Decimal('1442.24'))
        self.assertEqual(money('-12.30'), Decimal('-12.30'))
        self.assertEqual(money('0'), Decimal('0.00'))
        for bad in ('NaN', 'Infinity', '1.234', '1e3', ''):
            with self.assertRaises(ValueError):
                money(bad)

    def test_specs_are_combined_after_name_cleanup(self):
        self.assertEqual(summarize_items([('*金属*螺栓M4', 'M4*20'), ('*金属*螺栓M6', 'M6*30'),
                         ('*金属*螺栓M8', 'M8*40')]), '螺栓M4×20、M6×30等')
        self.assertEqual(summarize_items([('*金属*螺钉', 'M4*35')]), '螺钉M4×35')
        self.assertEqual(summarize_items([('*塑料*塑料烧杯', '塑料烧杯\n1000ML[有手柄]')]),
                         '塑料烧杯1000ML')
        self.assertEqual(clean_spec('M4 【200个】304'), 'M4 304')
        self.assertEqual(clean_spec('A+B＋C'), 'A和B和C')
        for spec in ('0.9kg/组', '0.9kg/\n组', '0.9kg／组'):
            self.assertEqual(clean_spec(spec), '')
            self.assertEqual(summarize_items([('*橡胶*硅胶', spec)]), '硅胶')

    def test_electronic_chinese_specs_replace_generic_project_names(self):
        self.assertEqual(summarize_items([('*电子*单片机', '四路可调降压模块+12V电源适配器')]),
                         '四路可调降压模块和12V电源适配器')
        self.assertEqual(summarize_items([('*电子*电子元件', '显示屏模块')]), '显示屏模块')
        self.assertEqual(summarize_items([('*电子*电子元件', 'Leonardo R3开发板+数据线')]),
                         'Leonardo R3开发板和数据线')
        self.assertEqual(summarize_items([('*电子*电子元器件', 'KCD1黑色2脚2档带0.5线11cm')]),
                         'KCD1黑色2脚2档带0.5线11cm')

    def test_wrapped_specs_belong_to_the_correct_item(self):
        def span(text, x, y):
            return dict(text=text, x0=x, x1=x + 40, y=y, h=10)
        entries = [span('项目名称', 40, 100), span('规格型号', 120, 100), span('单位', 200, 100),
                   span('*电子*开发', 10, 120), span('ESP32-DevKi', 120, 120), span('台', 200, 120),
                   span('板', 10, 140), span('tC', 120, 140),
                   span('*电子*价外费用', 10, 160), span('5.00', 400, 160)]
        items = items_from_page(entries)
        self.assertEqual(items, [('*电子*开发\n板', 'ESP32-DevKi\ntC'), ('*电子*价外费用', '')])
        self.assertEqual(summarize_items(items), '开发板ESP32-DevKitC')

    def test_subfolders_require_explicit_opt_in(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.build') as temp:
            folder = Path(temp)
            (folder / 'top.pdf').write_bytes(b'top')
            (folder / 'child').mkdir()
            (folder / 'child' / 'nested.pdf').write_bytes(b'nested')
            with patch('invoice_core.read_invoice', side_effect=lambda p: Invoice(p, p.stem, p.stem, Decimal(1))):
                self.assertEqual([i.path.name for i in scan_folder(folder)[0]], ['top.pdf'])
                self.assertEqual(len(scan_folder(folder, recursive=True)[0]), 2)

    def test_missing_data_is_not_zero(self):
        invoice = parse_pages('blank.pdf', [[]])
        self.assertIsNone(invoice.total)
        self.assertTrue(invoice.error)

    def parse_lines(self, lines):
        return parse_pages('test.pdf', [[{'text': t, 'x0': 0, 'x1': 300, 'y': i * 30, 'h': 10}
                                       for i, t in enumerate(lines)]])

    def test_only_lowercase_total_row_is_read(self):
        invoice = self.parse_lines(['发票号码：00000001', '单价 1.00 数量 999 金额 999.00',
                                    '合计 ￥999.00 ￥999.00', '价税合计（大写）壹拾贰元叁角肆分 （小写）￥12.34',
                                    '备注（小写）9999.00'])
        self.assertEqual(invoice.total, Decimal('12.34'))
        self.assertNotIn('金额与税额', invoice.error)
        invoice = self.parse_lines(['价税合计(大写)零元整 (小写)￥0.00'])
        self.assertEqual(invoice.total, Decimal('0'))

    def test_other_amounts_are_never_fallbacks(self):
        for lines in (['价税合计 ￥99.00'], ['（小写）￥88.00'],
                      ['价税合计（小写）', '￥77.00'], ['合计 ￥99.00 ￥1.00']):
            self.assertIsNone(self.parse_lines(lines).total)


class RenameFiles(unittest.TestCase):
    def test_collisions_duplicates_and_repeat_export(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.build') as temp:
            folder = Path(temp)
            occupied = folder / '螺母.pdf'
            occupied.write_bytes(b'keep')
            one, two, duplicate = [folder / n for n in ('a.pdf', 'b.pdf', 'copy.pdf')]
            for p in (one, two, duplicate):
                p.write_bytes(p.name.encode())
            invoices = [Invoice(one, '00000001', '螺母', Decimal(1)), Invoice(two, '00000002', '螺母', Decimal(2))]
            prepare_names(invoices)
            duplicates, renamed, errors = rename_pdfs(invoices, [(duplicate, one)])
            self.assertFalse(errors)
            self.assertEqual(len(renamed), 2)
            self.assertEqual(occupied.read_bytes(), b'keep')
            self.assertEqual([i.path.name for i in invoices], ['螺母（2）.pdf', '螺母（3）.pdf'])
            self.assertEqual([i.name for i in invoices], ['螺母', '螺母'])
            self.assertEqual([i.pdf_name for i in invoices], ['螺母（2）', '螺母（3）'])
            self.assertEqual(invoices[0].path.read_bytes(), b'a.pdf')
            self.assertEqual(duplicates[0][0], duplicate)
            self.assertTrue(duplicate.exists())
            self.assertEqual(duplicates[0][1], invoices[0].path)
            self.assertEqual(rename_pdfs(invoices, duplicates)[1:], ([], []))

    def test_filename_safety_and_missing_files(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.build') as temp:
            folder = Path(temp)
            one = folder / 'a.pdf'
            one.write_bytes(b'one')
            invoices = [Invoice(one, '00000001', '../测试:A/B*?', Decimal(1)),
                        Invoice(folder / 'missing.pdf', '00000002', '缺失', Decimal(2))]
            prepare_names(invoices)
            _, renamed, errors = rename_pdfs(invoices)
            self.assertEqual(len(renamed), 1)
            self.assertEqual(len(errors), 1)
            self.assertEqual(invoices[0].path.parent, folder)
            self.assertEqual(invoices[0].path.read_bytes(), b'one')
            self.assertEqual(invoices[0].pdf_name, invoices[0].path.stem)

    def test_safe_name_is_shared_with_workbook_and_collision_suffix(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.build') as temp:
            folder = Path(temp)
            source = folder / 'a.pdf'
            source.write_bytes(b'a')
            raw = 'A/B:C*D?E|F<G>H"\\.'
            safe = pdf_stem(raw)
            (folder / (safe + '.pdf')).write_bytes(b'occupied')
            invoice = Invoice(source, '00000001', raw, Decimal(2))
            out = folder / 'out.xlsx'
            export_bundle(ROOT / '报销清单表.xlsx', out, [invoice])
            self.assertEqual(invoice.name, safe)
            self.assertEqual(invoice.pdf_name, collision_name(safe, 2))
            self.assertEqual(invoice.path.stem, invoice.pdf_name)
            with ZipFile(out) as z:
                sheet = ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
                self.assertEqual(sheet.find(".//s:c[@r='D2']/s:is/s:t", N).text, invoice.name)
            self.assertEqual(pdf_stem('CON'), '_CON')
            self.assertEqual(len(pdf_stem('a' * 130)), 100)
            self.assertEqual(pdf_stem('测试[赠品]+价外费用A'), '测试和A')

    def test_duplicate_product_names_stay_duplicate_in_workbook(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.build') as temp:
            folder = Path(temp)
            one, two = folder / 'a.pdf', folder / 'b.pdf'
            one.write_bytes(b'a')
            two.write_bytes(b'b')
            invoices = [Invoice(one, '00000001', '滚动轴承5×8×2', Decimal(1)),
                        Invoice(two, '00000002', '滚动轴承5×8×2', Decimal(2))]
            out = folder / 'out.xlsx'
            export_bundle(ROOT / '报销清单表.xlsx', out, invoices)
            self.assertEqual([i.name for i in invoices], ['滚动轴承5×8×2', '滚动轴承5×8×2'])
            self.assertEqual([i.path.name for i in invoices], ['滚动轴承5×8×2.pdf', '滚动轴承5×8×2（2）.pdf'])
            with ZipFile(out) as z:
                sheet = ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
                self.assertEqual(sheet.find(".//s:c[@r='D2']/s:is/s:t", N).text, '滚动轴承5×8×2')
                self.assertEqual(sheet.find(".//s:c[@r='D3']/s:is/s:t", N).text, '滚动轴承5×8×2')

    def test_failed_rename_does_not_publish_workbook(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.build') as temp:
            folder = Path(temp)
            source = folder / 'a.pdf'
            source.write_bytes(b'unchanged')
            invoice = Invoice(source, '00000001', 'new', Decimal(2))
            out = folder / 'out.xlsx'
            with patch('invoice_core.Path.rename', side_effect=PermissionError('locked')):
                with self.assertRaises(OSError):
                    export_bundle(ROOT / '报销清单表.xlsx', out, [invoice])
            self.assertFalse(out.exists())
            self.assertEqual(source.read_bytes(), b'unchanged')
            self.assertEqual(list(folder.iterdir()), [source])

    def test_failed_workbook_publish_restores_original_names(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.build') as temp:
            folder = Path(temp)
            source = folder / 'a.pdf'
            source.write_bytes(b'unchanged')
            invoice = Invoice(source, '00000001', 'new', Decimal(2))
            out = folder / 'out.xlsx'
            out.write_bytes(b'old workbook')
            with patch('template_export.Path.replace', side_effect=PermissionError('locked')):
                with self.assertRaises(PermissionError):
                    export_bundle(ROOT / '报销清单表.xlsx', out, [invoice])
            self.assertEqual(out.read_bytes(), b'old workbook')
            self.assertEqual(invoice.path, source)
            self.assertTrue(source.exists())
            self.assertFalse((folder / 'new.pdf').exists())


class RealInvoices(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.invoices, cls.duplicates = scan_folder(ROOT / 'baoxiao/uploads')

    def test_sample_control_totals(self):
        self.assertEqual(len(self.invoices), 16)
        self.assertEqual(len(self.duplicates), 3)
        self.assertFalse([i.error for i in self.invoices if i.error])
        # Independent controls keyed by invoice number so renaming cannot change the check.
        controls = dict(zip(['26332000002049800341','26332000006359498746','26322000005888197516',
            '26332000006359489236','26442000006915246571','26342000001756670236','26322000004565999281',
            '26322000004567402486','26952000003771600481','26332000007637819611','26412000003356472826',
            '26952000003808060711','26112000003741772321','26312000005596912996','26322000007338983146',
            '26412000003354891211'], map(Decimal, ['2.20','1.88','32.67','5.16','64.70','44.00','9.19','9.19',
             '12.78','569.52','44.00','26.55','476.00','114.00','7.40','23.00'])))
        self.assertEqual({i.number: i.total for i in self.invoices}, controls)
        self.assertEqual(sum(i.total for i in self.invoices), Decimal('1442.24'))

    def test_real_specs_and_slash_exclusion(self):
        names = {i.number: i.name for i in self.invoices}
        self.assertEqual(names['26332000006359498746'], '螺钉M4×35')
        self.assertEqual(names['26312000005596912996'], '开发板ESP32-S3-DevKitC-1-N8R8')
        self.assertEqual(names['26112000003741772321'], '铂金硅胶ECOFLEX 00-30')
        self.assertEqual(names['26412000003354891211'], '四路可调降压模块和12V电源适配器')
        self.assertEqual(names['26952000003771600481'], '显示屏模块')
        self.assertEqual(names['26412000003356472826'], 'Leonardo R3开发板和数据线')
        self.assertEqual(names['26332000002049800341'], 'KCD1黑色2脚2档带0.5线11cm')
        self.assertEqual(names['26342000001756670236'], '螺钉螺栓M6')
        self.assertTrue(all('价外费用' not in name for name in names.values()))
        self.assertTrue(all(not any(char in name for char in '【】[]+＋（）') for name in names.values()))

    def test_template_export_preserves_format_and_identifiers(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.build') as temp:
            target = Path(temp) / 'test.xlsx'
            export_workbook(ROOT / '报销清单表.xlsx', target, self.invoices)
            with ZipFile(target) as z:
                root = ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
                rows = root.findall('s:sheetData/s:row', N)
                self.assertEqual(len(rows), 17)
                by_ref = {c.get('r'): c for r in rows for c in r}
                self.assertEqual(by_ref['C2'].get('t'), 'inlineStr')
                self.assertEqual(by_ref['C2'].findtext('s:is/s:t', namespaces=N), self.invoices[0].number)
                for row in range(2, 18):
                    for col in 'ABH':
                        self.assertEqual(len(by_ref[f'{col}{row}']), 0)
                self.assertFalse(root.findall('.//s:f', N))
                self.assertEqual({m.get('ref') for m in root.findall('s:mergeCells/s:mergeCell', N)}, {'A2:A17','B2:B17'})
                for i, invoice in enumerate(self.invoices, 2):
                    self.assertEqual(Decimal(by_ref[f'E{i}'].findtext('s:v', namespaces=N)), invoice.total)
                    self.assertEqual(by_ref[f'F{i}'].findtext('s:v', namespaces=N), '1')
                    self.assertEqual(Decimal(by_ref[f'G{i}'].findtext('s:v', namespaces=N)), invoice.total)
                with ZipFile(ROOT / '报销清单表.xlsx') as source:
                    source_root = ET.fromstring(source.read('xl/worksheets/sheet1.xml'))
                    self.assertEqual(ET.tostring(root.find('s:cols', N)), ET.tostring(source_root.find('s:cols', N)))
                    a = ET.fromstring(source.read('xl/styles.xml'))
                    b = ET.fromstring(z.read('xl/styles.xml'))
                    for section in ('fonts', 'fills', 'borders'):
                        self.assertEqual(ET.tostring(a.find(f's:{section}', N)), ET.tostring(b.find(f's:{section}', N)))

    def test_export_boundaries(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.build') as temp:
            target = Path(temp) / 'out.xlsx'
            for invoices in ([], [Invoice(Path('bad.pdf'), error='损坏')], self.invoices * 2):
                with self.assertRaises(ValueError):
                    export_workbook(ROOT / '报销清单表.xlsx', target, invoices)
                self.assertFalse(target.exists())
            with self.assertRaises(ValueError):
                export_workbook(ROOT / '报销清单表.xlsx', ROOT / '报销清单表.xlsx', self.invoices)


if __name__ == '__main__':
    unittest.main()
