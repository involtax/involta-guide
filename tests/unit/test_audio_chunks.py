import numpy as np
import pytest

from involta_voice.audio_chunks import fragmented_speech, pack_speech


@pytest.mark.parametrize(
    "segments, expected",
    [
        ([], False),
        ([(0, 1000), (2000, 3000), (4000, 5000)], False),
        ([(0, 1000), (2000, 3000), (4000, 5000), (6000, 7000)], True),
        ([(0, 2000), (3000, 5000), (6000, 8000), (9000, 11000)], False),
        ([(0, 2000), (3000, 5000), (6000, 8000), (9000, 10999)], True),
    ],
)
def test_only_repeated_short_speech_fragments_trigger_packing(segments, expected):
    assert fragmented_speech(segments, 1000) is expected


@pytest.mark.parametrize("pause_ms", [200, 300, 500])
def test_packing_preserves_every_speech_sample_in_order_with_original_gap_edges(pause_ms):
    waveform = np.arange(60000)
    segments = [(100, 10000), (30000, 35000), (36000, 40000)]
    original = list(segments)
    chunks = pack_speech(segments, 16000, 20, pause_ms)
    assert len(chunks) == 1
    packed = np.concatenate([waveform[a:b] for a, b in chunks[0]])
    assert len(packed) == 18900 + pause_ms * 16 + 1000
    # The kept source ranges neither duplicate nor reorder samples.
    assert np.all(packed[1:] > packed[:-1])
    for start, end in segments:
        assert np.isin(waveform[start:end], packed).all()
    edge = pause_ms * 16 // 2
    assert chunks == [[(100, 10000 + edge), (30000 - edge, 40000)]]
    assert segments == original


def test_packing_uses_actual_packed_length_and_never_splits_a_speech_interval():
    assert pack_speech([(0, 100), (1000, 1100), (2000, 2100)], 100, 3, 300) == [
        [(0, 115), (985, 1100)],
        [(2000, 2100)],
    ]


def test_short_gaps_and_continuous_speech_remain_bit_identical():
    assert pack_speech([(40, 140), (150, 230), (230, 250)], 1000, 20, 300) == [[(40, 250)]]
    assert pack_speech([(40, 140)], 1000, 20, 300) == [[(40, 140)]]
    assert pack_speech([], 1000, 20, 300) == []


def test_pauses_between_chunks_are_not_inserted_or_charged_to_the_limit():
    assert pack_speech([(0, 200), (5000, 5200)], 100, 2, 300) == [
        [(0, 200)],
        [(5000, 5200)],
    ]


@pytest.mark.parametrize("segments", [[(-1, 5)], [(2, 2)], [(0, 201)], [(5, 15), (10, 20)]])
def test_invalid_vad_ranges_fail_instead_of_dropping_or_duplicating_speech(segments):
    with pytest.raises(ValueError):
        pack_speech(segments, 100, 2, 300)


@pytest.mark.parametrize("rate,limit,pause", [(0, 20, 300), (16000, 0, 300), (16000, 20, -1)])
def test_invalid_limits_fail(rate, limit, pause):
    with pytest.raises(ValueError):
        pack_speech([], rate, limit, pause)
