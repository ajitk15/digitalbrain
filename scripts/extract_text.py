"""Convert a local byte stream to bounded Markdown with Microsoft MarkItDown.

    extract_text.py <file> <suffix> [<report.json>] [<tesseract>]

The text goes to stdout. With a third argument, what conversion saw is written
there as JSON: for a PDF, its page count and the pages that held no text. With
a fourth, the path of a Tesseract binary, pages with no text layer and images
are read by OCR - locally, by a program that is handed a file and makes no
network call - and the text says which pages were read that way.

When conversion is refused, the report says why instead (`failure_report`):
one reason from `REASONS` and the page counts behind it, so the person who
uploaded the file is told "this PDF is a scan" rather than "could not convert".
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
#: Why a conversion was refused, as the report names it. A closed set: the
#: server turns each into a sentence and ignores anything else.
REASONS = {
    "too_large", "archive_limits", "encrypted", "too_many_pages", "image_needs_ocr",
    "ocr_page_limit", "ocr_failed", "ocr_no_text", "scanned", "empty", "oversized",
}


class Refused(ValueError):
    """A conversion refused for a reason a person can be told: one of `REASONS`,
    and the facts that explain it (page counts, never text from the file)."""

    def __init__(self, message, reason, **facts):
        super().__init__(message)
        self.reason = reason
        self.facts = facts


def failure_report(error):
    """What the report says about a refusal: its reason and facts, or "unknown"
    when the error was not one this module raised on purpose."""
    if isinstance(error, Refused) and error.reason in REASONS:
        return {"failure": error.reason, **error.facts}
    return {"failure": "unknown"}


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
            raise Refused("OCR failed", "ocr_failed")
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
                raise Refused("OCR page limit", "ocr_page_limit", pages=frames)
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
        raise Refused("Input limit", "too_large")
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > 2000 or sum(x.file_size for x in members) > 30000000:
                raise Refused("Archive limits", "archive_limits")
    if suffix in IMAGES:
        if ocr is None:
            raise Refused("Images need OCR", "image_needs_ocr")
        with tempfile.TemporaryDirectory() as scratch:
            frames, (read, found, empty) = picture(path, ocr, scratch)
        result = "\n\n".join(read)
        if not result.strip() or len(result) > LIMIT:
            raise Refused("No text read", "oversized" if result.strip() else "ocr_no_text",
                          pages=frames)
        return result, finish({"pages": frames}, found, empty)
    report, read = {}, []
    if suffix == ".pdf":
        reader = PdfReader(path, strict=True)
        if reader.is_encrypted:
            raise Refused("PDF limits", "encrypted")
        if len(reader.pages) > 200:
            raise Refused("PDF limits", "too_many_pages", pages=len(reader.pages))
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
                raise Refused("OCR page limit", "ocr_page_limit", pages=len(blank))
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
        pages, blank = report.get("pages") or 0, report.get("pages_without_text") or []
        if not result.strip() and pages and len(blank) == pages:
            # Every page is a picture of text. Said as that, because "empty" is
            # what a person hears as "the file is broken".
            reason = "ocr_no_text" if ocr is not None else "scanned"
            raise Refused("Empty, binary or oversized conversion", reason, pages=pages)
        reason = "oversized" if len(result) > LIMIT else "empty"
        raise Refused("Empty, binary or oversized conversion", reason)
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
    except Exception as error:  # noqa: BLE001 - every failure exits 2; the report says which
        if len(sys.argv) > 3 and sys.argv[3]:
            Path(sys.argv[3]).write_text(json.dumps(failure_report(error)), encoding="utf-8")
        sys.exit(2)
