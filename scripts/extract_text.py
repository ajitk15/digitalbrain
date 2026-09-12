"""Convert a local byte stream to bounded Markdown with Microsoft MarkItDown."""

import socket
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch

from markitdown import MarkItDown, StreamInfo
from pypdf import PdfReader

LIMIT = 1000000


def extract(path, suffix):
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("Input limit")
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > 2000 or sum(x.file_size for x in members) > 30000000:
                raise ValueError("Archive limits")
    if suffix == ".pdf":
        reader = PdfReader(path, strict=True)
        if reader.is_encrypted or len(reader.pages) > 200:
            raise ValueError("PDF limits")
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
    return result


if __name__ == "__main__":
    try:
        value = extract(Path(sys.argv[1]), sys.argv[2])
        sys.stdout.buffer.write(value.encode("utf-8"))
    except Exception:
        sys.exit(2)
