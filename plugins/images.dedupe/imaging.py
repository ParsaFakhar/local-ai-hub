"""Pure-Python image decoding + perceptual hashing (stdlib only).

``images.dedupe`` must not depend on Pillow: the platform ships with zero
third-party Python packages, and a plugin that needs a compiler on the user's
machine is not a plugin you can just drop in.

What is implemented here
------------------------
* **PNG** — non-interlaced, bit depth 1/2/4/8/16, colour types 0/2/3/4/6
  (greyscale, RGB, palette, greyscale+alpha, RGBA), all five scanline filters.
* **JPEG** — baseline *and* progressive, any chroma subsampling, restart
  markers.  Only the **DC coefficient** of every 8x8 block is decoded, which is
  exactly the block's mean value: an eighth-resolution thumbnail for ~5% of the
  work of a full decode (no IDCT, no upsampling, no colour conversion).  AC
  coefficients are parsed to keep the bitstream aligned and then discarded.
* **Hashes** — aHash, dHash and pHash (DCT-II) over a box-resampled greyscale
  grid, plus Hamming distance.

Perceptual hashes only ever need a tiny greyscale thumbnail, so the DC-only
path is not a compromise — it is the right amount of decoding.

Not supported (reported per image, never fatal): Adam7-interlaced PNG,
JPEG-LS / lossless / arithmetic-coded / CMYK JPEG, GIF, WebP, AVIF, HEIF.
"""

from __future__ import annotations

import math
import struct
import zlib

__all__ = [
    "UnsupportedImage",
    "Gray",
    "decode",
    "average_hash",
    "difference_hash",
    "perceptual_hash",
    "hamming",
    "hash_hex",
    "hash_from_hex",
]

#: Refuse to allocate for absurd images (a decompression bomb, not a photo).
MAX_PIXELS = 40_000_000
#: Greyscale conversion subsamples above this many pixels; hashes need ~1 MP.
TARGET_PIXELS = 1_000_000


class UnsupportedImage(ValueError):
    """The bytes are not an image this decoder can read."""


class Gray:
    """A greyscale image: ``w * h`` values in ``data`` (row-major, 0..255)."""

    __slots__ = ("w", "h", "data")

    def __init__(self, w: int, h: int, data):
        self.w = w
        self.h = h
        self.data = data

    def resample(self, width: int, height: int) -> list[float]:
        """Separable area resampling to ``width x height``.

        Downscaling averages every source pixel that falls under the target
        cell (a true box filter, not nearest-neighbour); upscaling spreads a
        source pixel across the cells it overlaps, which degrades to linear
        interpolation.  Both directions matter here: a PNG arrives at full
        resolution while a JPEG arrives as an eighth-size DC thumbnail, and
        the two must hash to the same value.
        """
        if self.w <= 0 or self.h <= 0 or width <= 0 or height <= 0:
            return [0.0] * (max(0, width) * max(0, height))
        if (width, height) == (self.w, self.h):
            return [float(value) for value in self.data]

        xweights = _axis_weights(self.w, width)
        yweights = _axis_weights(self.h, height)
        data, w, h = self.data, self.w, self.h

        # horizontal pass: h rows of `width` values
        rows: list[list[float]] = []
        for y in range(h):
            base = y * w
            row = [0.0] * width
            for x, entries in enumerate(xweights):
                total = 0.0
                for index, weight in entries:
                    total += data[base + index] * weight
                row[x] = total
            rows.append(row)

        out: list[float] = []
        for entries in yweights:
            row = [0.0] * width
            for index, weight in entries:
                source = rows[index]
                for x in range(width):
                    row[x] += source[x] * weight
            out.extend(row)
        return out


def _axis_weights(source: int, target: int) -> list[list[tuple[int, float]]]:
    """Fractional-coverage weights mapping ``target`` cells onto ``source``."""
    table: list[list[tuple[int, float]]] = []
    scale = source / target
    for index in range(target):
        left = index * scale
        right = left + scale
        span = right - left
        start = int(math.floor(left))
        end = int(math.ceil(right))
        entries: list[tuple[int, float]] = []
        for position in range(start, min(end, source)):
            overlap = min(right, position + 1) - max(left, position)
            if overlap > 1e-9:
                entries.append((position, overlap / span))
        if not entries:
            entries = [(min(source - 1, max(0, start)), 1.0)]
        table.append(entries)
    return table


# ---------------------------------------------------------------------------
# PNG
# ---------------------------------------------------------------------------

_PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _unfilter(raw: bytes, height: int, stride: int, bpp: int) -> bytearray:
    out = bytearray(height * stride)
    prev = bytearray(stride)
    pos = 0
    for y in range(height):
        if pos >= len(raw):
            raise UnsupportedImage("truncated PNG data")
        kind = raw[pos]
        pos += 1
        line = bytearray(raw[pos : pos + stride])
        if len(line) != stride:
            raise UnsupportedImage("truncated PNG scanline")
        pos += stride
        if kind == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif kind == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif kind == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif kind == 4:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                upleft = prev[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + _paeth(left, prev[i], upleft)) & 0xFF
        elif kind != 0:
            raise UnsupportedImage(f"unknown PNG filter {kind}")
        out[y * stride : (y + 1) * stride] = line
        prev = line
    return out


def _samples(line: bytearray, count: int, depth: int):
    """Yield ``count`` samples of ``depth`` bits from one scanline."""
    if depth == 8:
        return line[:count]
    if depth == 16:
        return [line[i * 2] for i in range(count)]  # high byte is enough
    per_byte = 8 // depth
    mask = (1 << depth) - 1
    out = []
    for index in range(count):
        byte = line[index // per_byte]
        shift = 8 - depth * (index % per_byte + 1)
        out.append((byte >> shift) & mask)
    return out


def _decode_png(data: bytes) -> Gray:
    pos = 8
    width = height = depth = ctype = 0
    interlace = 0
    palette = b""
    idat = bytearray()
    while pos + 8 <= len(data):
        length = int.from_bytes(data[pos : pos + 4], "big")
        kind = data[pos + 4 : pos + 8]
        chunk = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, depth, ctype, _comp, _filt, interlace = struct.unpack(">IIBBBBB", chunk)
        elif kind == b"PLTE":
            palette = chunk
        elif kind == b"IDAT":
            idat += chunk
        elif kind == b"IEND":
            break
    if not width or not height:
        raise UnsupportedImage("PNG without IHDR")
    if interlace:
        raise UnsupportedImage("interlaced (Adam7) PNG is not supported")
    if ctype not in _PNG_CHANNELS:
        raise UnsupportedImage(f"unknown PNG colour type {ctype}")
    if depth not in (1, 2, 4, 8, 16):
        raise UnsupportedImage(f"unsupported PNG bit depth {depth}")
    if ctype in (0, 3) and depth not in (1, 2, 4, 8, 16):
        raise UnsupportedImage("invalid PNG depth for this colour type")
    if width * height > MAX_PIXELS:
        raise UnsupportedImage("PNG is too large to hash")

    channels = _PNG_CHANNELS[ctype]
    stride = (width * channels * depth + 7) // 8
    bpp = max(1, (channels * depth + 7) // 8)
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        raise UnsupportedImage(f"PNG zlib error: {exc}") from exc
    pixels = _unfilter(raw, height, stride, bpp)

    scale = 255 // ((1 << depth) - 1) if depth < 8 else 1
    step = max(1, int(math.sqrt((width * height) / TARGET_PIXELS)))
    out_w = (width + step - 1) // step
    out_h = (height + step - 1) // step
    gray: list[int] = []
    for y in range(0, height, step):
        line = pixels[y * stride : (y + 1) * stride]
        row = _samples(line, width * channels, depth)
        for x in range(0, width, step):
            base = x * channels
            if ctype == 3:
                index = row[base]
                offset = index * 3
                if offset + 2 < len(palette):
                    r, g, b = palette[offset], palette[offset + 1], palette[offset + 2]
                else:
                    r = g = b = 0
            elif ctype in (0, 4):
                r = g = b = row[base] * scale
            else:
                r, g, b = row[base] * scale, row[base + 1] * scale, row[base + 2] * scale
            gray.append((r * 299 + g * 587 + b * 114) // 1000)
    return Gray(out_w, out_h, gray)


# ---------------------------------------------------------------------------
# JPEG (DC-only)
# ---------------------------------------------------------------------------

_ZIGZAG_FIRST = 0  # the DC coefficient is always first in zigzag order


class _Huffman:
    """Canonical JPEG Huffman decoder (ITU T.81 F.16)."""

    __slots__ = ("mincode", "maxcode", "valptr", "symbols")

    def __init__(self, counts: list[int], symbols: bytes):
        self.symbols = symbols
        self.mincode = [0] * 17
        self.maxcode = [-1] * 17
        self.valptr = [0] * 17
        code = 0
        index = 0
        for length in range(1, 17):
            self.valptr[length] = index
            self.mincode[length] = code
            code += counts[length - 1]
            index += counts[length - 1]
            self.maxcode[length] = code - 1 if counts[length - 1] else -1
            code <<= 1

    def decode(self, reader: "_Bits") -> int:
        code = 0
        for length in range(1, 17):
            code = (code << 1) | reader.bit()
            if self.maxcode[length] >= 0 and code <= self.maxcode[length]:
                position = self.valptr[length] + code - self.mincode[length]
                if 0 <= position < len(self.symbols):
                    return self.symbols[position]
                raise UnsupportedImage("corrupt JPEG Huffman table")
        raise UnsupportedImage("invalid JPEG Huffman code")


class _Bits:
    """MSB-first bit reader over one entropy-coded segment."""

    __slots__ = ("data", "pos", "bitpos", "size")

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0
        self.bitpos = 0
        self.size = len(data)

    def bit(self) -> int:
        if self.pos >= self.size:
            return 0  # decoders pad with zeros past the end of a scan
        value = (self.data[self.pos] >> (7 - self.bitpos)) & 1
        self.bitpos += 1
        if self.bitpos == 8:
            self.bitpos = 0
            self.pos += 1
        return value

    def receive(self, count: int) -> int:
        value = 0
        for _ in range(count):
            value = (value << 1) | self.bit()
        return value


def _extend(value: int, count: int) -> int:
    if count == 0:
        return 0
    if value < (1 << (count - 1)):
        return value - (1 << count) + 1
    return value


def _segments(data: bytes, start: int) -> tuple[list[bytes], int]:
    """Split entropy data at restart markers; return (segments, next marker)."""
    segments: list[bytes] = []
    current = bytearray()
    pos = start
    size = len(data)
    while pos < size:
        byte = data[pos]
        if byte != 0xFF:
            current.append(byte)
            pos += 1
            continue
        nxt = data[pos + 1] if pos + 1 < size else 0xD9
        if nxt == 0x00:  # byte stuffing
            current.append(0xFF)
            pos += 2
        elif 0xD0 <= nxt <= 0xD7:  # restart marker
            segments.append(bytes(current))
            current = bytearray()
            pos += 2
        elif nxt == 0xFF:  # fill byte
            pos += 1
        else:
            break
    segments.append(bytes(current))
    return segments, pos


def _decode_jpeg(data: bytes) -> Gray:
    width = height = 0
    sof_kind = None
    components: list[dict] = []
    dc_tables: dict[int, _Huffman] = {}
    ac_tables: dict[int, _Huffman] = {}
    quant_dc: dict[int, int] = {}
    restart_interval = 0
    scans: list[dict] = []

    pos = 2  # past SOI
    size = len(data)
    while pos + 4 <= size:
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        pos += 2
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            continue
        if marker == 0xD9:  # EOI
            break
        if pos + 2 > size:
            break
        length = int.from_bytes(data[pos : pos + 2], "big")
        payload = data[pos + 2 : pos + length]
        after = pos + length

        if marker in (0xC0, 0xC1, 0xC2):
            sof_kind = marker
            precision = payload[0]
            height = int.from_bytes(payload[1:3], "big")
            width = int.from_bytes(payload[3:5], "big")
            count = payload[5]
            components = []
            for index in range(count):
                offset = 6 + index * 3
                if offset + 2 >= len(payload) + 1 and offset + 2 > len(payload):
                    break
                cid = payload[offset]
                sampling = payload[offset + 1]
                components.append(
                    {
                        "id": cid,
                        "h": max(1, sampling >> 4),
                        "v": max(1, sampling & 0x0F),
                        "tq": payload[offset + 2],
                        "pred": 0,
                        "dc": {},
                        "coef": {},
                    }
                )
            if precision != 8:
                raise UnsupportedImage(f"{precision}-bit JPEG is not supported")
            if count not in (1, 3):
                raise UnsupportedImage(f"JPEG with {count} components is not supported")
        elif marker in (0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            raise UnsupportedImage("only baseline and progressive JPEG are supported")
        elif marker == 0xC4:  # DHT
            offset = 0
            while offset < len(payload):
                header = payload[offset]
                table_class, table_id = header >> 4, header & 0x0F
                counts = list(payload[offset + 1 : offset + 17])
                total = sum(counts)
                symbols = payload[offset + 17 : offset + 17 + total]
                table = _Huffman(counts, symbols)
                (dc_tables if table_class == 0 else ac_tables)[table_id] = table
                offset += 17 + total
        elif marker == 0xDB:  # DQT
            offset = 0
            while offset < len(payload):
                header = payload[offset]
                precision, table_id = header >> 4, header & 0x0F
                offset += 1
                if precision:
                    quant_dc[table_id] = int.from_bytes(payload[offset : offset + 2], "big")
                    offset += 128
                else:
                    quant_dc[table_id] = payload[offset]
                    offset += 64
        elif marker == 0xDD:  # DRI
            restart_interval = int.from_bytes(payload[0:2], "big")
        elif marker == 0xDA:  # SOS
            count = payload[0]
            scan_components = []
            for index in range(count):
                offset = 1 + index * 2
                cid = payload[offset]
                tables = payload[offset + 1]
                component = next((c for c in components if c["id"] == cid), None)
                if component is None:
                    raise UnsupportedImage("JPEG scan references an unknown component")
                scan_components.append((component, tables >> 4, tables & 0x0F))
            tail = 1 + count * 2
            ss, se = payload[tail], payload[tail + 1]
            approx = payload[tail + 2]
            segments, next_pos = _segments(data, after)
            scans.append(
                {
                    "components": scan_components,
                    "ss": ss,
                    "se": se,
                    "ah": approx >> 4,
                    "al": approx & 0x0F,
                    "segments": segments,
                    # Tables are redefined between scans (progressive files do
                    # this constantly): keep the ones that were in force here.
                    "dc_tables": dict(dc_tables),
                    "ac_tables": dict(ac_tables),
                    "quant_dc": dict(quant_dc),
                }
            )
            pos = next_pos
            continue
        pos = after

    if not components or not width or not height or sof_kind is None:
        raise UnsupportedImage("JPEG without a frame header")
    if width * height > MAX_PIXELS:
        raise UnsupportedImage("JPEG is too large to hash")

    hmax = max(c["h"] for c in components)
    vmax = max(c["v"] for c in components)
    mcus_x = (width + 8 * hmax - 1) // (8 * hmax)
    mcus_y = (height + 8 * vmax - 1) // (8 * vmax)
    luma = components[0]
    blocks_x = mcus_x * luma["h"]
    blocks_y = mcus_y * luma["v"]

    for scan in scans:
        if scan["ss"] != 0:
            continue  # AC-only progressive scan: nothing we need
        scan_components = scan["components"]
        interleaved = len(scan_components) > 1
        if interleaved:
            total_units = mcus_x * mcus_y
        else:
            component = scan_components[0][0]
            comp_w = (width * component["h"] + hmax - 1) // hmax
            comp_h = (height * component["v"] + vmax - 1) // vmax
            total_units = ((comp_w + 7) // 8) * ((comp_h + 7) // 8)
        scan_dc_tables = scan["dc_tables"]
        scan_ac_tables = scan["ac_tables"]
        scan_quant = scan["quant_dc"]

        unit = 0
        for segment in scan["segments"]:
            reader = _Bits(segment)
            if scan["ah"] == 0:
                # A new DC scan starts a fresh prediction chain (also after
                # every restart marker).  Refinement scans instead continue
                # from the coefficients the previous scan left behind.
                for component, _td, _ta in scan_components:
                    component["pred"] = 0
            limit = total_units if restart_interval <= 0 else min(total_units, unit + restart_interval)
            while unit < limit:
                if interleaved:
                    placements = []
                    for component, td, ta in scan_components:
                        for v in range(component["v"]):
                            for h in range(component["h"]):
                                bx = (unit % mcus_x) * component["h"] + h
                                by = (unit // mcus_x) * component["v"] + v
                                placements.append((component, td, ta, bx, by))
                else:
                    component, td, ta = scan_components[0]
                    per_row = max(1, (blocks_x if component is luma else
                                      ((width * component["h"] + hmax - 1) // hmax + 7) // 8))
                    placements = [(component, td, ta, unit % per_row, unit // per_row)]

                for component, td, ta, bx, by in placements:
                    if scan["ah"] == 0:
                        table = scan_dc_tables.get(td)
                        if table is None:
                            raise UnsupportedImage("JPEG scan without a DC Huffman table")
                        count = table.decode(reader)
                        diff = _extend(reader.receive(count), count)
                        component["pred"] += diff << scan["al"]
                        value = component["pred"]
                    else:
                        # Successive-approximation refinement: one raw bit per
                        # block, applied to the coefficient already decoded.
                        value = component["coef"].get((bx, by), 0)
                        if reader.bit():
                            value |= 1 << scan["al"]
                    if component is luma:
                        component["coef"][(bx, by)] = value
                        scale = scan_quant.get(component["tq"], 1) or 1
                        component["dc"][(bx, by)] = value * scale
                    if scan["se"] > 0:  # baseline: walk the AC coefficients
                        ac = scan_ac_tables.get(ta)
                        if ac is None:
                            raise UnsupportedImage("JPEG scan without an AC Huffman table")
                        index = 1
                        while index < 64:
                            symbol = ac.decode(reader)
                            run, size = symbol >> 4, symbol & 0x0F
                            if size == 0:
                                if run != 15:
                                    break  # end of block
                                index += 16
                            else:
                                index += run + 1
                                reader.receive(size)
                unit += 1
            if unit >= total_units:
                break

    if not luma["dc"]:
        raise UnsupportedImage("JPEG contained no DC scan")

    # DC value = 8 * mean(pixel - 128) for the standard JPEG DCT.
    gray = [0] * (blocks_x * blocks_y)
    for (bx, by), value in luma["dc"].items():
        if 0 <= bx < blocks_x and 0 <= by < blocks_y:
            level = value // 8 + 128
            gray[by * blocks_x + bx] = 0 if level < 0 else (255 if level > 255 else level)
    return Gray(blocks_x, blocks_y, gray)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def decode(data: bytes) -> Gray:
    """Decode PNG or JPEG bytes into a small greyscale image."""
    if not data or len(data) < 8:
        raise UnsupportedImage("empty image data")
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return _decode_png(data)
    if data[:2] == b"\xff\xd8":
        return _decode_jpeg(data)
    if data[:6] in (b"GIF87a", b"GIF89a"):
        raise UnsupportedImage("GIF is not supported (re-encode as PNG or JPEG)")
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        raise UnsupportedImage("WebP is not supported (re-encode as PNG or JPEG)")
    if data[4:8] == b"ftyp":
        raise UnsupportedImage("HEIF/AVIF is not supported (re-encode as PNG or JPEG)")
    raise UnsupportedImage("unrecognised image format (expected PNG or JPEG)")


# ---------------------------------------------------------------------------
# hashes
# ---------------------------------------------------------------------------


def _bits_to_int(bits) -> int:
    value = 0
    for bit in bits:
        value = (value << 1) | (1 if bit else 0)
    return value


def average_hash(image: Gray, size: int = 8) -> int:
    grid = image.resample(size, size)
    mean = sum(grid) / len(grid)
    return _bits_to_int(value > mean for value in grid)


def difference_hash(image: Gray, size: int = 8) -> int:
    grid = image.resample(size + 1, size)
    bits = []
    for row in range(size):
        base = row * (size + 1)
        for column in range(size):
            bits.append(grid[base + column] > grid[base + column + 1])
    return _bits_to_int(bits)


_DCT_CACHE: dict[tuple[int, int], list[list[float]]] = {}


def _dct_matrix(n: int, limit: int) -> list[list[float]]:
    key = (n, limit)
    cached = _DCT_CACHE.get(key)
    if cached is None:
        cached = [
            [
                math.cos((2 * x + 1) * u * math.pi / (2 * n)) * (math.sqrt(1 / n) if u == 0 else math.sqrt(2 / n))
                for x in range(n)
            ]
            for u in range(limit)
        ]
        _DCT_CACHE[key] = cached
    return cached


def perceptual_hash(image: Gray, size: int = 32, hash_size: int = 8) -> int:
    """DCT pHash: low-frequency ``hash_size x hash_size`` block, median threshold."""
    grid = image.resample(size, size)
    matrix = _dct_matrix(size, hash_size)
    # rows first: temp[u][y]
    temp = [[0.0] * size for _ in range(hash_size)]
    for u in range(hash_size):
        row = matrix[u]
        for y in range(size):
            base = y * size
            temp[u][y] = sum(row[x] * grid[base + x] for x in range(size))
    coefficients = []
    for v in range(hash_size):
        column = matrix[v]
        for u in range(hash_size):
            coefficients.append(sum(column[y] * temp[u][y] for y in range(size)))
    body = coefficients[1:]  # drop the DC term: it only encodes overall brightness
    ordered = sorted(body)
    middle = len(ordered) // 2
    median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    return _bits_to_int(value > median for value in body)


def hamming(left: int, right: int) -> int:
    return bin(left ^ right).count("1")


def hash_hex(value: int, bits: int = 64) -> str:
    return format(value, f"0{bits // 4}x")


def hash_from_hex(text: str) -> int:
    cleaned = str(text).strip().lower().removeprefix("0x")
    if not cleaned or any(char not in "0123456789abcdef" for char in cleaned):
        raise ValueError(f"not a hexadecimal hash: {text!r}")
    return int(cleaned, 16)
