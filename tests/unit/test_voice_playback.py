import hashlib
import io

import av
import pytest

from involta_voice.playback import playback_response, seekable_webm


def test_seekable_webm_fixes_duration_without_reencoding_or_changing_original(streaming_webm):
    source = streaming_webm.read_bytes()
    with av.open(io.BytesIO(source)) as container:
        assert container.duration is None
        original_packets = [bytes(p) for p in container.demux(audio=0) if p.dts is not None]
    result = seekable_webm(streaming_webm)
    assert result == seekable_webm(streaming_webm)
    with av.open(io.BytesIO(result)) as container:
        assert abs(container.duration / av.time_base - 6.2) < 0.05
        packets = [bytes(p) for p in container.demux(audio=0) if p.dts is not None]
    assert packets == original_packets
    assert streaming_webm.read_bytes() == source
    response = playback_response(result, "bytes=20-99")
    assert response.status_code == 206 and response.body == result[20:100]
    assert response.headers["content-range"] == f"bytes 20-99/{len(result)}"
    assert response.headers["etag"] == '"' + hashlib.sha256(result).hexdigest() + '"'


@pytest.mark.parametrize(
    ("header", "status", "expected"),
    [
        ("bytes=2-4", 206, b"234"),
        ("bytes=7-", 206, b"789"),
        ("bytes=-3", 206, b"789"),
        ("bytes=0-999", 206, b"0123456789"),
        ("bytes=50-", 416, b""),
        ("bytes=7-2", 416, b""),
        ("bytes=-0", 416, b""),
        ("bytes=-", 416, b""),
        ("bytes=0-1,4-5", 200, b"0123456789"),
        ("bad", 200, b"0123456789"),
        ("bytes=" + "9" * 5000 + "-", 200, b"0123456789"),
    ],
)
def test_playback_range_bounds(header, status, expected):
    response = playback_response(b"0123456789", header)
    assert response.status_code == status and response.body == expected
    if status == 416:
        assert response.headers["content-range"] == "bytes */10"


def test_if_range_mismatch_returns_complete_representation():
    response = playback_response(b"0123456789", "bytes=3-5", '"outdated"')
    assert response.status_code == 200 and response.body == b"0123456789"
