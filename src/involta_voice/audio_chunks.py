"""Build ASR context from VAD intervals without changing speech speed or samples."""


def fragmented_speech(segments: list[tuple[int, int]], sample_rate: int) -> bool:
    """Conservative v1 gate: repeated short fragments, not ordinary phrase pauses."""
    return len(segments) >= 4 and sum(end - start for start, end in segments) < (
        2 * sample_rate * len(segments)
    )


def pack_speech(
    segments: list[tuple[int, int]], sample_rate: int, max_duration_s: float, pause_ms: int
) -> list[list[tuple[int, int]]]:
    """Return original sample ranges for bounded chunks, keeping both sides of pauses.

    Every detected speech interval is retained in full and in order. Only the
    middle of a long non-speech gap is omitted. Ranges still address the original
    waveform, so a shortened ASR timeline never pretends to be the original one.
    """
    limit = int(max_duration_s * sample_rate)
    pause = pause_ms * sample_rate // 1000
    if sample_rate <= 0 or limit <= 0 or pause_ms < 0:
        raise ValueError("Invalid ASR chunk limits")
    chunks: list[list[tuple[int, int]]] = []
    current: list[tuple[int, int]] = []
    duration = 0
    previous_end = 0
    for start, end in segments:
        if start < previous_end or start < 0 or end <= start or end - start > limit:
            raise ValueError("Invalid or oversized VAD interval")
        length = end - start
        gap = min(start - previous_end, pause) if current else 0
        if current and duration + gap + length > limit:
            chunks.append(current)
            current = []
            duration = 0
            gap = 0
        if current:
            after = gap // 2
            before = gap - after
            last_start, last_end = current[-1]
            if start - previous_end <= pause:
                current[-1] = (last_start, end)
            else:
                current[-1] = (last_start, last_end + after)
                current.append((start - before, end))
        else:
            current = [(start, end)]
        duration += gap + length
        previous_end = end
    if current:
        chunks.append(current)
    return chunks
