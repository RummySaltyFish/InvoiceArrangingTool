import csv
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from invoice_sorter import SortItem, organize_items, split_groups_by_500


ROOT = Path(__file__).resolve().parents[1]
BUILD_DIR = ROOT / '.build'
BUILD_DIR.mkdir(exist_ok=True)


class AmountSorting(unittest.TestCase):
    def item(self, path, total):
        return SortItem(Path(path), None if total is None else Decimal(total), "文本",
                        "金额未识别" if total is None else "")

    def test_groups_never_mix_below_and_at_least_500(self):
        items = [self.item(f"{index}.pdf", value) for index, value in enumerate(
            ("10", "499.99", "500", "600", "700"), 1)]
        groups = split_groups_by_500(items, 2)
        self.assertEqual([[item.total for item in group] for group in groups], [
            [Decimal("10"), Decimal("499.99")],
            [Decimal("500"), Decimal("600")],
            [Decimal("700")],
        ])
        for group in groups:
            self.assertFalse(any(item.total < 500 for item in group)
                             and any(item.total >= 500 for item in group))

    def test_organize_copies_files_and_writes_manifest(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.build') as temp:
            base = Path(temp)
            source, output = base / "source", base / "output"
            source.mkdir()
            files = []
            for name, data in (("a.pdf", b"a"), ("b.pdf", b"b"), ("bad.pdf", b"bad")):
                path = source / name
                path.write_bytes(data)
                files.append(path)
            items = [SortItem(files[0], Decimal("100"), "文本"),
                     SortItem(files[1], Decimal("500"), "文本"),
                     SortItem(files[2], None, "文本", "未识别金额")]
            recognized, unknown, manifest = organize_items(items, output, group_size=15)
            self.assertEqual((recognized, unknown), (2, 1))
            self.assertTrue(all(path.exists() for path in files))
            group_folders = sorted(path.name for path in output.iterdir() if path.is_dir())
            self.assertEqual(len(group_folders), 3)
            self.assertTrue(group_folders[0].startswith("001_"))
            self.assertTrue(group_folders[1].startswith("002_"))
            self.assertEqual(group_folders[2], "999_未识别金额")
            with manifest.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 3)
            self.assertEqual([row["状态"] for row in rows], ["已整理", "已整理", "金额未识别"])
            self.assertEqual([row["金额"] for row in rows[:2]], ["100.00", "500.00"])


if __name__ == "__main__":
    unittest.main()
