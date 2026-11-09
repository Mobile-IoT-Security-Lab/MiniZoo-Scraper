import sys
from pathlib import Path
import shutil
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from ocr_tesseract import parse_tsv, recognize

try:
    import PIL
    HAS_OCR = int(PIL.__version__.split(".")[0]) >= 13 and bool(shutil.which("tesseract"))
except ImportError:
    HAS_OCR = False

HEADER = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
PAGE = "1\t1\t0\t0\t0\t0\t0\t0\t1080\t2400\t-1\t\n"


class TesseractTests(unittest.TestCase):
    def test_unicode_words_share_line_and_keep_pixel_bounds(self):
        data = HEADER + PAGE + (
            "5\t1\t1\t1\t1\t1\t10\t20\t30\t40\t95\t微信\n"
            "5\t1\t1\t1\t1\t2\t45\t20\t50\t40\t85\tSearch\n"
            "5\t1\t2\t1\t1\t1\t10\t90\t80\t35\t90\tCancel\n")
        result = parse_tsv(data)
        self.assertEqual((result["width"], result["height"]), (1080, 2400))
        self.assertEqual(result["nodes"][0]["text"], "微信 Search")
        self.assertEqual(result["nodes"][0]["bounds"], [10, 20, 95, 60])
        self.assertAlmostEqual(result["nodes"][0]["confidence"], 0.875)
        self.assertEqual(len(result["nodes"]), 2)

    def test_rejects_out_of_image_and_invalid_confidences(self):
        rows = "".join(f"5\t1\t{i}\t1\t1\t1\t{x}\t0\t100\t10\t{c}\tBad\n"
                       for i, (x, c) in enumerate([(1000, 90), (-5, 90), (0, -1), (0, "nan")]))
        self.assertEqual(parse_tsv(HEADER + PAGE + rows)["nodes"], [])

    def test_blank_image_is_valid_but_missing_dimensions_fails(self):
        self.assertEqual(parse_tsv(HEADER + PAGE)["nodes"], [])
        with self.assertRaises(ValueError):
            parse_tsv(HEADER)

    def test_adjacent_chinese_words_join_but_badge_gap_remains(self):
        data = HEADER + PAGE + (
            "5\t1\t1\t1\t1\t1\t10\t20\t50\t30\t95\t住房\n"
            "5\t1\t1\t1\t1\t2\t64\t20\t75\t30\t95\t公积金\n"
            "5\t1\t1\t1\t1\t3\t160\t25\t50\t20\t95\t政府\n")
        self.assertEqual(parse_tsv(data)["nodes"][0]["text"], "住房公积金 政府")

    def test_symbol_prefix_preserves_label_word_box_and_its_own_confidence(self):
        data = HEADER + PAGE + (
            "5\t1\t1\t1\t1\t1\t40\t194\t26\t46\t92\t《\n"
            "5\t1\t1\t1\t1\t2\t109\t164\t7\t106\t91\t|\n"
            "5\t1\t1\t1\t1\t3\t142\t200\t137\t36\t90\tSearch\n")
        result = parse_tsv(data)
        self.assertEqual(len(result['nodes']), 1)
        line = result['nodes'][0]
        self.assertEqual(line['text'], '《 | Search')
        self.assertEqual(line['bounds'], [40, 164, 279, 270])
        self.assertEqual(line['words'][-1],
                         {'text': 'Search', 'confidence': 0.9, 'bounds': [142, 200, 279, 236]})

    @unittest.skipUnless(HAS_OCR, "Requires the Docker OCR dependencies")
    def test_dark_placeholder_is_confident_and_source_geometry_unchanged(self):
        from PIL import Image, ImageDraw, ImageFont
        image = Image.new("RGB", (1080, 1000), (17, 17, 17))
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default(size=40)
        draw.text((350, 70), "Mini Programs", font=font, fill=(210, 210, 210))
        draw.rectangle((20, 180, 1060, 270), fill=(27, 27, 27))
        draw.text((300, 200), "Search Mini Programs", font=font, fill=(95, 95, 95))
        draw.text((60, 360), "Recently Used", font=font, fill=(210, 210, 210))
        draw.rectangle((70, 470, 200, 600), fill=(255, 255, 255))
        draw.text((60, 700), "My Commonly Used", font=font, fill=(210, 210, 210))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dark.png"
            image.save(path)
            original = path.read_bytes()
            screen = recognize(path, language="eng")
            self.assertEqual(path.read_bytes(), original)
        self.assertEqual((screen["width"], screen["height"]), (1080, 1000))
        matches = [n for n in screen["nodes"]
                   if n["text"] == "Search Mini Programs" and n["confidence"] >= 0.8]
        self.assertTrue(matches, screen)
        for node in matches:
            left, top, right, bottom = node["bounds"]
            self.assertTrue(280 <= left < right <= 1060 and 180 <= top < bottom <= 270)


if __name__ == "__main__":
    unittest.main()
