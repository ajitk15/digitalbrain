"""Convert a local byte stream to bounded Markdown with Microsoft MarkItDown.

    extract_text.py <file> <suffix> [<report.json>]

The text goes to stdout. With a third argument, what conversion saw is written
there as JSON: for a PDF, its page count and the pages that held no text.
"""

import json
import socket
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch

from markitdown import MarkItDown, StreamInfo
from pypdf import PdfReader

LIMIT = 1000000


def extract_with_report(path, suffix):
    """(Markdown, report). The report is read from the file, never from the text."""
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("Input limit")
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > 2000 or sum(x.file_size for x in members) > 30000000:
                raise ValueError("Archive limits")
    report = {}
    if suffix == ".pdf":
        reader = PdfReader(path, strict=True)
        if reader.is_encrypted or len(reader.pages) > 200:
            raise ValueError("PDF limits")
        # A scanned page has no text layer. MarkItDown skips it without a word,
        # so it is counted here, where a person can be told.
        report = {
            "pages": len(reader.pages),
            "pages_without_text": [
                number
                for number, page in enumerate(reader.pages, 1)
                if not (page.extract_text() or "").strip()
            ],
        }
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
    if not result.strip() or len(result) > LIMIT or "\x00" in result:
        raise ValueError("Empty, binary or oversized conversion")
    return result, report


def extract(path, suffix):
    return extract_with_report(path, suffix)[0]


if __name__ == "__main__":
    try:
        value, found = extract_with_report(Path(sys.argv[1]), sys.argv[2])
        if len(sys.argv) > 3:
            Path(sys.argv[3]).write_text(json.dumps(found), encoding="utf-8")
        sys.stdout.buffer.write(value.encode("utf-8"))
    except Exception:
        sys.exit(2)
