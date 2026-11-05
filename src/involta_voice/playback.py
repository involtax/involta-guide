"""Seekable WebM representation; originals remain byte-for-byte intact."""

import hashlib
import io
import re
from pathlib import Path

from fastapi.responses import Response


def seekable_webm(path: Path) -> bytes:
    import av

    result = io.BytesIO()
    with (
        av.open(str(path)) as source,
        av.open(result, "w", format="webm", options={"fflags": "+bitexact"}) as target,
    ):
        audio = source.streams.audio[0]
        stream = target.add_stream_from_template(audio)
        for packet in source.demux(audio):
            if packet.dts is None:
                continue
            packet.stream = stream
            target.mux(packet)
    return result.getvalue()


def playback_response(data: bytes, range_header: str = "", if_range: str = "") -> Response:
    etag = '"' + hashlib.sha256(data).hexdigest() + '"'
    headers = {"Cache-Control": "private,no-store", "Accept-Ranges": "bytes", "ETag": etag}
    match = re.fullmatch(r"bytes=(\d{0,20})-(\d{0,20})", range_header)
    # A server may ignore unsupported units/multipart ranges. If-Range mismatch
    # requires the complete representation, never a range from another version.
    if match and (not if_range or if_range == etag):
        first, last = match.groups()
        size = len(data)
        if not first and not last:
            start, end = size, size
        elif first:
            start, end = int(first), min(int(last), size - 1) if last else size - 1
        else:
            length = int(last)
            start, end = max(0, size - length) if length else size, size - 1
        if start > end or start >= size:
            return Response(
                status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"}
            )
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return Response(
            data[start : end + 1], status_code=206, media_type="audio/webm", headers=headers
        )
    return Response(data, media_type="audio/webm", headers=headers)
