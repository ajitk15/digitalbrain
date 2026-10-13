"""Scanned pages and images are read by a local OCR, only when a deployment says so."""

import json
import shutil
import subprocess
import tempfile
from io import BytesIO
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from digitalbrain import configuration
from platform_core import processing, source_quality
from platform_core.models import Document, KnowledgeEntry
from scripts import extract_text
from scripts.extract_text import extract_with_report, words_of

from . import test_document_originals
from .test_document_originals import PDF, SETTINGS, with_blank_page

TSV = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
    "1\t1\t0\t0\t0\t0\t0\t0\t100\t100\t-1\t\n"
    "5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t96\tUrgent\n"
    "5\t1\t1\t1\t1\t2\t0\t0\t10\t10\t90\treferrals\n"
    "5\t1\t1\t1\t2\t1\t0\t0\t10\t10\t84\tescalate\n"
    "5\t1\t2\t1\t1\t1\t0\t0\t10\t10\t70\tBR-02\n"
    "5\t1\t2\t1\t1\t2\t0\t0\t10\t10\t-1\t\n"
)


def reader(text="Urgent referrals are acknowledged within 4 hours.", confidence=91):
    """Stands in for Tesseract: records what it was handed, returns fixed text."""
    seen = []

    def read(image):
        seen.append(Path(image))
        return text, confidence

    read.seen = seen
    return read


def picture(path, size=(400, 200), frames=1):
    from PIL import Image

    images = [Image.new("RGB", size, "white") for _ in range(frames)]
    images[0].save(path, save_all=frames > 1, append_images=images[1:])
    return path


class ExtractionTests(SimpleTestCase):
    def test_tesseract_tsv_becomes_lines_and_a_mean_confidence(self):
        text, confidence = words_of(TSV)
        self.assertEqual(text, "Urgent referrals\nescalate\n\nBR-02")
        self.assertEqual(confidence, 85)
        self.assertEqual(words_of(""), ("", 0))

    def test_a_blank_pdf_page_is_rendered_and_read(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "scan.pdf"
            path.write_bytes(with_blank_page(PDF))
            read = reader()
            text, report = extract_with_report(path, ".pdf", read)
        last = report["pages"]
        self.assertEqual(len(read.seen), 1)
        self.assertEqual(report["ocr_pages"], [{"page": last, "confidence": 91}])
        self.assertEqual(report["pages_without_text"], [])
        self.assertEqual(report["low_confidence_pages"], [])
        self.assertIn(f"## Page {last} (read by OCR, 91% confidence)", text)
        self.assertIn("acknowledged within 4 hours", text)

    def test_a_page_ocr_finds_nothing_on_stays_a_page_without_text(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "scan.pdf"
            path.write_bytes(with_blank_page(PDF))
            _, report = extract_with_report(path, ".pdf", reader(text=""))
        self.assertEqual(report["pages_without_text"], [report["pages"]])
        self.assertEqual(report["ocr_pages"], [])

    def test_pages_with_a_text_layer_are_never_sent_to_ocr(self):
        read = reader()
        _, report = extract_with_report(PDF, ".pdf", read)
        self.assertEqual(read.seen, [])
        self.assertNotIn("ocr_pages", report)

    def test_more_scanned_pages_than_the_cap_fail_the_document(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "scan.pdf"
            path.write_bytes(with_blank_page(PDF))
            with patch.object(extract_text, "MAX_OCR_PAGES", 0):
                with self.assertRaisesMessage(ValueError, "OCR page limit"):
                    extract_with_report(path, ".pdf", reader())

    def test_an_image_needs_ocr(self):
        with tempfile.TemporaryDirectory() as folder:
            path = picture(Path(folder) / "board.png")
            with self.assertRaisesMessage(ValueError, "Images need OCR"):
                extract_with_report(path, ".png")
            text, report = extract_with_report(path, ".png", reader(confidence=41))
        self.assertIn("## Page 1 (read by OCR, 41% confidence)", text)
        self.assertEqual(report["pages"], 1)
        self.assertEqual(report["low_confidence_pages"], [1])

    def test_an_image_ocr_reads_nothing_from_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            path = picture(Path(folder) / "board.png")
            with self.assertRaisesMessage(ValueError, "No text read"):
                extract_with_report(path, ".png", reader(text=""))

    def test_each_frame_of_a_tiff_is_a_page_up_to_the_cap(self):
        with tempfile.TemporaryDirectory() as folder:
            path = picture(Path(folder) / "fax.tiff", frames=3)
            read = reader()
            _, report = extract_with_report(path, ".tiff", read)
            self.assertEqual(len(read.seen), 3)
            self.assertEqual([page["page"] for page in report["ocr_pages"]], [1, 2, 3])
            with patch.object(extract_text, "MAX_OCR_PAGES", 2):
                with self.assertRaisesMessage(ValueError, "OCR page limit"):
                    extract_with_report(path, ".tiff", reader())

    def test_a_decompression_bomb_is_refused_not_read(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as folder:
            path = picture(Path(folder) / "huge.png", size=(300, 300))
            # Between the limit and twice it Pillow only warns; here that is an error.
            with patch.object(Image, "MAX_IMAGE_PIXELS", 60000):
                with self.assertRaises(Image.DecompressionBombWarning):
                    extract_with_report(path, ".png", reader())

    def test_the_cap_is_the_same_on_both_sides_of_the_subprocess(self):
        self.assertEqual(processing.MAX_OCR_PAGES, extract_text.MAX_OCR_PAGES)
        self.assertEqual(source_quality.LOW_CONFIDENCE, extract_text.LOW_CONFIDENCE)


class CheckTests(SimpleTestCase):
    TEXT = "# Referral service levels\n\n" + "Urgent referrals are acknowledged. " * 10

    def readable(self, report):
        return next(check for check in source_quality.assess(self.TEXT, report)["checks"]
                    if check.key == "readable")

    def test_pages_read_well_by_ocr_say_so_and_pass(self):
        check = self.readable({"pages": 2, "pages_without_text": [],
                               "ocr_pages": [{"page": 1, "confidence": 90},
                                             {"page": 2, "confidence": 80}],
                               "low_confidence_pages": []})
        self.assertEqual(check.state, "good")
        self.assertIn("2 of them by OCR at 85% confidence", check.finding)

    def test_a_poorly_read_page_is_missing_and_asks_for_a_better_scan(self):
        check = self.readable({"pages": 2, "pages_without_text": [],
                               "ocr_pages": [{"page": 1, "confidence": 90},
                                             {"page": 2, "confidence": 40}],
                               "low_confidence_pages": [2]})
        self.assertEqual(check.state, "missing")
        self.assertIn("under 60% confidence (page 2)", check.finding)
        self.assertIn("300 dpi", check.improve)

    def test_with_ocr_off_a_blank_page_suggests_turning_it_on(self):
        check = self.readable({"pages": 2, "pages_without_text": [2]})
        self.assertIn("turn on OCR", check.improve)


class SettingTests(SimpleTestCase):
    def load(self, text):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "local.toml"
            config.write_text('mode = "development"\nhosts = ["localhost"]\n' + text)
            with patch.dict("os.environ", {"DIGITAL_BRAIN_CONFIG": str(config)}):
                return configuration.load_config()

    def test_ocr_must_be_a_boolean_and_the_binary_an_absolute_path(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "ocr must be a boolean"):
            self.load('ocr = "yes"\n')
        with self.assertRaisesMessage(ImproperlyConfigured, "must be an absolute path"):
            self.load('ocr = true\ntesseract_path = "tesseract"\n')
        self.assertTrue(self.load("ocr = true\n")["ocr"])

    @override_settings(OCR_ENABLED=False, TESSERACT_PATH=__file__)
    def test_off_means_no_binary_even_when_one_is_named(self):
        self.assertEqual(processing.tesseract_path(), "")
        self.assertFalse(processing.ocr_ready())

    @override_settings(OCR_ENABLED=True, TESSERACT_PATH=__file__)
    def test_on_uses_the_named_binary(self):
        self.assertEqual(processing.tesseract_path(), __file__)
        self.assertTrue(processing.ocr_ready())

    @override_settings(OCR_ENABLED=True, TESSERACT_PATH="")
    def test_on_without_a_binary_is_not_ready(self):
        with patch("platform_core.processing.shutil.which", return_value=None):
            self.assertFalse(processing.ocr_ready())


def in_process(read):
    """`subprocess.run` for the extractor, run here with a stand-in OCR."""
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        path, suffix, report_path, binary = args[2:6]
        try:
            text, report = extract_with_report(Path(path), suffix, read if binary else None)
        except ValueError:
            return subprocess.CompletedProcess(args, 1, b"", b"")
        Path(report_path).write_text(json.dumps(report), encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, text.encode("utf-8"), b"")

    run.calls = calls
    return run


@override_settings(**SETTINGS)
class PipelineTests(TestCase):
    setUp = test_document_originals.OriginalTests.setUp
    upload = test_document_originals.OriginalTests.upload
    convert = test_document_originals.OriginalTests.convert

    @override_settings(OCR_ENABLED=True, TESSERACT_PATH=__file__)
    def test_with_ocr_on_a_scanned_page_is_read_and_the_page_says_so(self):
        run = in_process(reader())
        document = self.upload("scan.pdf", with_blank_page(PDF))
        with patch("platform_core.processing.subprocess.run", run):
            self.convert()
        args, kwargs = run.calls[0]
        self.assertEqual(args[-1], __file__)
        self.assertEqual(kwargs["timeout"], processing.OCR_CONVERSION_TIMEOUT)
        document.refresh_from_db()
        self.assertEqual(document.status, "ready")
        self.assertEqual(len(document.conversion_report["ocr_pages"]), 1)
        entry = KnowledgeEntry.objects.get(document=document)
        self.assertIn("read by OCR", entry.content)
        readable = next(c for c in entry.quality["checks"] if c["key"] == "readable")
        self.assertEqual(readable["state"], "good")
        page = self.client.get(reverse("document-detail", args=[self.app.pk, document.pk]))
        self.assertContains(page, "1 read by OCR")

    @override_settings(OCR_ENABLED=False)
    def test_with_ocr_off_an_image_fails_and_says_why(self):
        run = in_process(reader())
        document = self.upload("board.png", self.png())
        with patch("platform_core.processing.subprocess.run", run):
            self.convert()
        args, kwargs = run.calls[0]
        self.assertEqual(args[-1], "")
        self.assertEqual(kwargs["timeout"], processing.CONVERSION_TIMEOUT)
        document.refresh_from_db()
        self.assertEqual(document.status, "failed")
        self.assertIn("need OCR, which is not on", document.conversion_error)
        self.assertFalse(Document.objects.filter(status="ready").exists())

    def png(self):
        from PIL import Image

        out = BytesIO()
        Image.new("RGB", (40, 20), "white").save(out, format="PNG")
        return out.getvalue()


@skipUnless(shutil.which("tesseract"), "Tesseract is not installed here; the Dockerfile has it")
class RealTesseractTests(SimpleTestCase):
    def test_tesseract_reads_a_rendered_line(self):
        from PIL import Image, ImageDraw, ImageFont

        try:
            font = ImageFont.load_default(size=48)
        except TypeError:
            font = ImageFont.load_default()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "line.png"
            image = Image.new("RGB", (1400, 200), "white")
            ImageDraw.Draw(image).text((40, 60), "Referral acknowledged", fill="black",
                                       font=font)
            image.save(path)
            text, report = extract_with_report(
                path, ".png", extract_text.tesseract(shutil.which("tesseract"))
            )
        self.assertIn("Referral", text)
        self.assertEqual(report["pages"], 1)
