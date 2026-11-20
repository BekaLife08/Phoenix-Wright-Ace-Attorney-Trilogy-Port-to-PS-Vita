"""Single-sample FSB5 reader and fresh HEVAG container writer (no sample data)."""
from dataclasses import dataclass
import struct

RATES = (4000, 8000, 11000, 11025, 16000, 22050, 24000, 32000, 44100, 48000, 96000)


@dataclass(frozen=True)
class Sample:
    frames: int
    channels: int
    rate: int
    codec: int
    loop: tuple[int, int] | None  # start inclusive, end exclusive


def parse(data: bytes) -> Sample:
    if len(data) < 68 or data[:4] != b'FSB5':
        raise ValueError('Expected FSB5')
    version, count, headers, names, size, codec = struct.unpack_from('<6I', data, 4)
    if version != 1 or count != 1:
        raise ValueError('Only FSB5 v1 with one sample is supported')
    if headers < 8 or 60 + headers + names + size != len(data):
        raise ValueError('Invalid FSB5 sizes')
    word, = struct.unpack_from('<Q', data, 60)
    frames = word >> 34
    if ((word >> 7) & 0x7ffffff) != 0:
        raise ValueError('Unexpected single-sample data offset')
    channels = (1, 2, 6, 8)[(word >> 5) & 3]
    index = (word >> 1) & 15
    if index >= len(RATES):
        raise ValueError('Unsupported FSB5 rate index')
    rate, loop, more, pos = RATES[index], None, word & 1, 68
    while more:
        if pos + 4 > 60 + headers:
            raise ValueError('Truncated FSB5 chunk')
        chunk, = struct.unpack_from('<I', data, pos)
        pos += 4
        kind, length, more = chunk >> 25, (chunk >> 1) & 0xffffff, chunk & 1
        end = pos + length
        if end > 60 + headers:
            raise ValueError('Invalid FSB5 chunk size')
        if kind == 1:
            if length != 1:
                raise ValueError('Invalid channel override')
            channels = data[pos]
        elif kind == 2:
            if length != 4:
                raise ValueError('Invalid rate override')
            rate, = struct.unpack_from('<I', data, pos)
        elif kind == 3:
            if length != 8 or loop is not None:
                raise ValueError('Invalid loop chunk')
            start, last = struct.unpack_from('<2I', data, pos)
            loop = (start, last + 1)
        pos = end
    if frames < 1 or channels not in (1, 2) or rate < 1:
        raise ValueError('Only nonempty mono/stereo samples are supported')
    if loop is not None and not (0 <= loop[0] < loop[1] <= frames):
        raise ValueError('FSB5 loop is outside the sample')
    return Sample(frames, channels, rate, codec, loop)


def hevag(raw: bytes, sample: Sample) -> bytes:
    if not (0 < sample.frames < 1 << 30) or sample.channels not in (1, 2):
        raise ValueError('Invalid HEVAG sample layout')
    if len(raw) != ((sample.frames + 27) // 28) * 16 * sample.channels:
        raise ValueError('Wrong HEVAG frame count')
    extra = b''
    rate_index = RATES.index(sample.rate) if sample.rate in RATES else 0
    chunks = []
    if sample.rate not in RATES:
        if not (0 < sample.rate <= 192000):
            raise ValueError('Unsupported sample rate')
        chunks.append((2, struct.pack('<I', sample.rate)))
    if sample.loop is not None:
        start, end = sample.loop
        if not (0 <= start < end <= sample.frames):
            raise ValueError('Invalid HEVAG loop')
        chunks.append((3, struct.pack('<2I', start, end - 1)))
    for i, (kind, payload) in enumerate(chunks):
        extra += struct.pack('<I', (kind << 25) | (len(payload) << 1) | (i + 1 < len(chunks))) + payload
    word = (sample.frames << 34) | ((sample.channels - 1) << 5) | (rate_index << 1) | bool(chunks)
    headers = struct.pack('<Q', word) + extra
    headers += bytes((-60 - len(headers)) % 32)
    header = struct.pack('<4s6I', b'FSB5', 1, 1, len(headers), 0, len(raw), 9) + bytes(32)
    result = header + headers + raw
    if parse(result) != Sample(sample.frames, sample.channels, sample.rate, 9, sample.loop):
        raise ValueError('HEVAG container verification failed')
    return result
