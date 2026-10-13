"""Convert a local byte stream to bounded Markdown with Microsoft MarkItDown.

    extract_text.py <file> <suffix> [<report.json>] [<tesseract>]

The text goes to stdout. With a third argument, what conversion saw is written
there as JSON: for a PDF, its page count and the pages that held no text. With
a fourth, the path of a Tesseract binary, pages with no text layer and images
are read by OCR - locally, by a program that is handed a file and makes no
network call - and the text says which pages were read that way.
"""

import json
import socket
import subprocess
import sys
import tempfile
import warnings
import zipfile
from collections import defaultdict
from pathlib import Path
from unittest.mock import patch

from markitdown import MarkItDown, StreamInfo
from pypdf import PdfReader

LIMIT = 1000000
#: Pictures of text. Converted only with OCR on; without it there is nothing to read.
IMAGES = {".png", ".jpg", ".jpeg", ".tif", ".tiff"}
#: Pages read by OCR in one document. Over it, the document fails and says why.
MAX_OCR_PAGES = 50
#: Below this mean word confidence a page is reported as hard to read.
LOW_CONFIDENCE = 60
#: 300 dpi: what Tesseract is trained on. PDF user space is 72 per inch.
RENDER_SCALE = 300 / 72
OCR_TIMEOUT = 60


def tesseract(binary):
    """A reader of image files: (text, mean word confidence 0-100), by Tesseract."""

    def read(image):
        done = subprocess.run(
            [binary, str(image), "stdout", "-l", "eng", "tsv"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=OCR_TIMEOUT,
            check=False,
        )
        if done.returncode != 0:
            raise ValueError("OCR failed")
        return words_of(done.stdout.decode("utf-8", "replace"))

    return read


def words_of(tsv):
    """Tesseract's TSV as lines of text, paragraphs apart, and its mean confidence."""
    rows = tsv.splitlines()
    if not rows:
        return "", 0
    header = rows[0].split("\t")
    lines, confidences = defaultdict(list), []
    for row in rows[1:]:
        cells = row.split("\t")
        if len(cells) != len(header):
            continue
        cell = dict(zip(header, cells, strict=True))
        word = cell.get("text", "").strip()
        try:
            confidence = float(cell.get("conf", "-1"))
            where = (int(cell["block_num"]), int(cell["par_num"]), int(cell["line_num"]))
        except (KeyError, ValueError):
            continue
        if cell.get("level") != "5" or not word or confidence < 0:
            continue
        lines[where].append(word)
        confidences.append(confidence)
    text, paragraph = [], None
    for where in sorted(lines):
        if paragraph is not None and where[:2] != paragraph:
            text.append("")
        paragraph = where[:2]
        text.append(" ".join(lines[where]))
    mean = round(sum(confidences) / len(confidences)) if confidences else 0
    return "\n".join(text).strip(), mean


def ocr_pages(images, ocr):
    """Read each (page number, image) and record what was found."""
    read, found, empty = [], [], []
    for number, image in images:
        text, confidence = ocr(image)
        if text:
            read.append(f"## Page {number} (read by OCR, {confidence}% confidence)\n\n{text}")
            found.append({"page": number, "confidence": confidence})
        else:
            empty.append(number)
    return read, found, empty


def scanned_pdf(path, blank, ocr, scratch):
    """Render each page with no text layer and read it."""
    import pypdfium2

    document = pypdfium2.PdfDocument(str(path))
    try:
        images = []
        for number in blank:
            image = Path(scratch) / f"page-{number}.png"
            document[number - 1].render(scale=RENDER_SCALE).to_pil().save(image)
            images.append((number, image))
        return ocr_pages(images, ocr)
    finally:
        document.close()


def picture(path, ocr, scratch):
    """Each frame of an image file, read by OCR. Pillow's bomb guard is an error here."""
    from PIL import Image

    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(path) as image:
            frames = getattr(image, "n_frames", 1)
            if frames > MAX_OCR_PAGES:
                raise ValueError("OCR page limit")
            images = []
            for frame in range(frames):
                image.seek(frame)
                target = Path(scratch) / f"page-{frame + 1}.png"
                image.convert("RGB").save(target)
                images.append((frame + 1, target))
    return frames, ocr_pages(images, ocr)


def finish(report, found, empty):
    report["ocr_pages"] = found
    report["low_confidence_pages"] = [
        page["page"] for page in found if page["confidence"] < LOW_CONFIDENCE
    ]
    report["pages_without_text"] = empty
    return report


def extract_with_report(path, suffix, ocr=None):
    """(Markdown, report). The report is read from the file, never from the text.

    `ocr` reads an image file and returns (text, confidence); None means OCR is
    off, and a page with no text layer is reported rather than read.
    """
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("Input limit")
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > 2000 or sum(x.file_size for x in members) > 30000000:
                raise ValueError("Archive limits")
    if suffix in IMAGES:
        if ocr is None:
            raise ValueError("Images need OCR")
        with tempfile.TemporaryDirectory() as scratch:
            frames, (read, found, empty) = picture(path, ocr, scratch)
        result = "\n\n".join(read)
        if not result.strip() or len(result) > LIMIT:
            raise ValueError("No text read")
        return result, finish({"pages": frames}, found, empty)
    report, read = {}, []
    if suffix == ".pdf":
        reader = PdfReader(path, strict=True)
        if reader.is_encrypted or len(reader.pages) > 200:
            raise ValueError("PDF limits")
        # A scanned page has no text layer. MarkItDown skips it without a word,
        # so it is counted here, where a person can be told - and read, with OCR on.
        blank = [
            number
            for number, page in enumerate(reader.pages, 1)
            if not (page.extract_text() or "").strip()
        ]
        report = {"pages": len(reader.pages), "pages_without_text": blank}
        if ocr is not None and blank:
            if len(blank) > MAX_OCR_PAGES:
                raise ValueError("OCR page limit")
            with tempfile.TemporaryDirectory() as scratch:
                read, found, empty = scanned_pdf(path, blank, ocr, scratch)
            report = finish(report, found, empty)
    # Conversion only receives the stored stream, not a URL. No LLM, plugins or network calls.
    with (
        path.open("rb") as source,
        patch.object(socket.socket, "connect", side_effect=OSError("Offline conversion")),
        patch.object(socket.socket, "connect_ex", side_effect=OSError("Offline conversion")),
    ):
        converter = MarkItDown(enable_plugins=False)
        result = converter.convert_stream(
            source, stream_info=StreamInfo(extension=suffix or None)
        ).text_content
    if read:
        result = "\n\n".join([result.strip(), *read]).strip()
    if not result.strip() or len(result) > LIMIT or "\x00" in result:
        raise ValueError("Empty, binary or oversized conversion")
    return result, report


def extract(path, suffix):
    return extract_with_report(path, suffix)[0]


if __name__ == "__main__":
    try:
        binary = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] else None
        value, found = extract_with_report(
            Path(sys.argv[1]), sys.argv[2], tesseract(binary) if binary else None
        )
        if len(sys.argv) > 3 and sys.argv[3]:
            Path(sys.argv[3]).write_text(json.dumps(found), encoding="utf-8")
        sys.stdout.buffer.write(value.encode("utf-8"))
    except Exception:
        sys.exit(2)
