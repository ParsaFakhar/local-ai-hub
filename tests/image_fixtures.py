"""Synthetic image fixtures for the test suite (stdlib only).

The perceptual-hash plugin ships its own PNG/JPEG decoder, so the tests need
real image bytes — but the repository must not carry binary fixtures and the
test suite must not depend on Pillow.  These tiny encoders produce valid PNG
and JPEG data on the fly:

* PNG: any colour type, bit depth 1/2/4/8, all five scanline filters.
* JPEG: baseline or progressive (DC scans), greyscale or 4:4:4 / 4:2:0 colour,
  optional restart markers.  Huffman tables use fixed-length codes, which is
  legal, trivially correct and keeps this file small.

They are deliberately independent of ``plugins/images.dedupe/imaging.py``:
the encoder does a full forward DCT and quantisation, the decoder only reads
DC coefficients back.
"""

from __future__ import annotations

import math
import struct
import zlib

# ITU-T T.81 Annex K luminance quantisation table (zigzag order not applied).
LUMA_QUANT = [
    16, 11, 10, 16, 24, 40, 51, 61,
    12, 12, 14, 19, 26, 58, 60, 55,
    14, 13, 16, 24, 40, 57, 69, 56,
    14, 17, 22, 29, 51, 87, 80, 62,
    18, 22, 37, 56, 68, 109, 103, 77,
    24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101,
    72, 92, 95, 98, 112, 100, 103, 99,
]

ZIGZAG = [
    0, 1, 8, 16, 9, 2, 3, 10,
    17, 24, 32, 25, 18, 11, 4, 5,
    12, 19, 26, 33, 40, 48, 41, 34,
    27, 20, 13, 6, 7, 14, 21, 28,
    35, 42, 49, 56, 57, 50, 43, 36,
    29, 22, 15, 23, 30, 37, 44, 51,
    58, 59, 52, 45, 38, 31, 39, 46,
    53, 60, 61, 54, 47, 55, 62, 63,
]


# ---------------------------------------------------------------------------
# PNG
# ---------------------------------------------------------------------------


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def png_bytes(width, height, pixel, color_type=2, bit_depth=8, filters=None):
    """Encode a PNG.  ``pixel(x, y)`` returns an (r, g, b) tuple."""
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    palette = b""
    if color_type == 3:
        palette = b"".join(bytes((index, index, index)) for index in range(256))

    max_value = (1 << bit_depth) - 1
    raw = bytearray()
    bpp = max(1, (channels * bit_depth + 7) // 8)
    previous = bytearray((width * channels * bit_depth + 7) // 8)
    for y in range(height):
        samples = []
        for x in range(width):
            r, g, b = pixel(x, y)
            if color_type in (0, 4):
                luma = (r * 299 + g * 587 + b * 114) // 1000
                value = (luma * max_value) // 255
                samples.append(value)
                if color_type == 4:
                    samples.append(max_value)
            elif color_type == 3:
                samples.append(((r * 299 + g * 587 + b * 114) // 1000) * max_value // 255)
            else:
                samples.append((r * max_value) // 255)
                samples.append((g * max_value) // 255)
                samples.append((b * max_value) // 255)
                if color_type == 6:
                    samples.append(max_value)

        line = bytearray(len(previous))
        if bit_depth == 8:
            line = bytearray(samples)
        else:
            per_byte = 8 // bit_depth
            for index, value in enumerate(samples):
                line[index // per_byte] |= (value & max_value) << (8 - bit_depth * (index % per_byte + 1))

        kind = (filters[y % len(filters)] if filters else y % 5)
        encoded = bytearray(line)
        if kind == 1:
            for i in range(len(line) - 1, bpp - 1, -1):
                encoded[i] = (line[i] - line[i - bpp]) & 0xFF
        elif kind == 2:
            for i in range(len(line)):
                encoded[i] = (line[i] - previous[i]) & 0xFF
        elif kind == 3:
            for i in range(len(line)):
                left = line[i - bpp] if i >= bpp else 0
                encoded[i] = (line[i] - ((left + previous[i]) >> 1)) & 0xFF
        elif kind == 4:
            for i in range(len(line)):
                left = line[i - bpp] if i >= bpp else 0
                upleft = previous[i - bpp] if i >= bpp else 0
                p = left + previous[i] - upleft
                pa, pb, pc = abs(p - left), abs(p - previous[i]), abs(p - upleft)
                pred = left if (pa <= pb and pa <= pc) else (previous[i] if pb <= pc else upleft)
                encoded[i] = (line[i] - pred) & 0xFF
        raw.append(kind)
        raw += encoded
        previous = line

    out = bytearray(b"\x89PNG\r\n\x1a\n")
    out += _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, 0))
    if palette:
        out += _chunk(b"PLTE", palette)
    out += _chunk(b"IDAT", zlib.compress(bytes(raw), 6))
    out += _chunk(b"IEND", b"")
    return bytes(out)


# ---------------------------------------------------------------------------
# JPEG
# ---------------------------------------------------------------------------


def _scale_quant(quality: int) -> list[int]:
    quality = max(1, min(100, quality))
    factor = 5000 // quality if quality < 50 else 200 - quality * 2
    return [max(1, min(255, (value * factor + 50) // 100)) for value in LUMA_QUANT]


_COS = [[math.cos((2 * x + 1) * u * math.pi / 16) for x in range(8)] for u in range(8)]


def _fdct(block: list[float]) -> list[float]:
    out = [0.0] * 64
    for v in range(8):
        for u in range(8):
            total = 0.0
            for y in range(8):
                row = block[y * 8]
                cy = _COS[v][y]
                for x in range(8):
                    total += block[y * 8 + x] * _COS[u][x] * cy
            cu = 1 / math.sqrt(2) if u == 0 else 1.0
            cv = 1 / math.sqrt(2) if v == 0 else 1.0
            out[v * 8 + u] = 0.25 * cu * cv * total
    return out


class _Writer:
    """MSB-first bit writer with JPEG byte stuffing."""

    def __init__(self):
        self.data = bytearray()
        self.buffer = 0
        self.count = 0

    def write(self, value: int, length: int) -> None:
        for shift in range(length - 1, -1, -1):
            self.buffer = (self.buffer << 1) | ((value >> shift) & 1)
            self.count += 1
            if self.count == 8:
                byte = self.buffer & 0xFF
                self.data.append(byte)
                if byte == 0xFF:
                    self.data.append(0x00)
                self.buffer = 0
                self.count = 0

    def flush(self) -> None:
        while self.count:
            self.write(1, 1)  # pad with 1 bits, as the spec requires


def _huffman_table(symbols):
    """Fixed-length canonical codes: counts[7] (8-bit codes) = len(symbols)."""
    ordered = sorted(set(symbols))
    counts = [0] * 16
    counts[7] = len(ordered)
    codes = {symbol: (index, 8) for index, symbol in enumerate(ordered)}
    return counts, ordered, codes


def _category(value: int) -> int:
    magnitude = abs(value)
    size = 0
    while magnitude:
        size += 1
        magnitude >>= 1
    return size


def _amplitude(value: int, size: int) -> int:
    return value if value >= 0 else value + (1 << size) - 1


def _dht(table_class: int, table_id: int, counts, symbols) -> bytes:
    payload = bytes([table_class << 4 | table_id]) + bytes(counts) + bytes(symbols)
    return b"\xff\xc4" + struct.pack(">H", len(payload) + 2) + payload


def _block_coefficients(plane, plane_w, plane_h, bx, by, quant):
    block = []
    for y in range(8):
        sy = min(plane_h - 1, by * 8 + y)
        for x in range(8):
            sx = min(plane_w - 1, bx * 8 + x)
            block.append(plane[sy * plane_w + sx] - 128.0)
    spectrum = _fdct(block)
    return [int(round(spectrum[ZIGZAG[index]] / quant[index])) for index in range(64)]


def jpeg_bytes(
    width,
    height,
    pixel,
    *,
    quality=92,
    color=True,
    sampling=(1, 1),
    progressive=False,
    restart_interval=0,
):
    """Encode a JPEG.  ``pixel(x, y)`` returns (r, g, b).

    ``sampling`` is the Y component's (h, v) factors: (1, 1) is 4:4:4,
    (2, 2) is 4:2:0.  Progressive files carry DC scans only (a first scan plus
    a refinement scan), which is exactly what a DC-only decoder consumes.
    """
    quant = _scale_quant(quality)
    hmax, vmax = sampling

    # ---- planes -----------------------------------------------------------
    y_w, y_h = width, height
    y_plane = [0.0] * (y_w * y_h)
    for yy in range(y_h):
        for xx in range(y_w):
            r, g, b = pixel(xx, yy)
            y_plane[yy * y_w + xx] = 0.299 * r + 0.587 * g + 0.114 * b
    planes = [(1, hmax, vmax, y_plane, y_w, y_h)]
    if color:
        c_w = (width + hmax - 1) // hmax
        c_h = (height + vmax - 1) // vmax
        cb_plane = [0.0] * (c_w * c_h)
        cr_plane = [0.0] * (c_w * c_h)
        for yy in range(c_h):
            for xx in range(c_w):
                rs = gs = bs = 0
                count = 0
                for dy in range(vmax):
                    for dx in range(hmax):
                        sx, sy = xx * hmax + dx, yy * vmax + dy
                        if sx < width and sy < height:
                            r, g, b = pixel(sx, sy)
                            rs += r
                            gs += g
                            bs += b
                            count += 1
                count = count or 1
                r, g, b = rs / count, gs / count, bs / count
                cb_plane[yy * c_w + xx] = 128 - 0.168736 * r - 0.331264 * g + 0.5 * b
                cr_plane[yy * c_w + xx] = 128 + 0.5 * r - 0.418688 * g - 0.081312 * b
        planes.append((2, 1, 1, cb_plane, c_w, c_h))
        planes.append((3, 1, 1, cr_plane, c_w, c_h))

    mcus_x = (width + 8 * hmax - 1) // (8 * hmax)
    mcus_y = (height + 8 * vmax - 1) // (8 * vmax)

    # ---- coefficients -----------------------------------------------------
    coefficients = {}
    for cid, h, v, plane, plane_w, plane_h in planes:
        blocks_x = mcus_x * h
        blocks_y = mcus_y * v
        table = {}
        for by in range(blocks_y):
            for bx in range(blocks_x):
                table[(bx, by)] = _block_coefficients(plane, plane_w, plane_h, bx, by, quant)
        coefficients[cid] = table

    out = bytearray(b"\xff\xd8")
    out += b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    out += b"\xff\xdb" + struct.pack(">H", 67) + b"\x00" + bytes(quant[ZIGZAG[i]] for i in range(64))

    sof = b"\xff\xc2" if progressive else b"\xff\xc0"
    payload = bytes([8]) + struct.pack(">HH", height, width) + bytes([len(planes)])
    for cid, h, v, _plane, _w, _h in planes:
        payload += bytes([cid, (h << 4) | v, 0])
    out += sof + struct.pack(">H", len(payload) + 2) + payload
    if restart_interval:
        out += b"\xff\xdd" + struct.pack(">H", 4) + struct.pack(">H", restart_interval)

    def emit_scan(component_specs, ss, se, ah, al, dc_split=False):
        """component_specs: [(cid, h, v)] in scan order."""
        nonlocal out
        dc_symbols, ac_symbols = set(), set()
        predictors = {cid: 0 for cid, _h, _v in component_specs}
        # Pre-compute the symbol sets so the Huffman tables cover every symbol
        # (including the ones that only appear after a restart reset).
        units = _scan_units(component_specs, mcus_x, mcus_y, width, height, hmax, vmax)
        blocks = _scan_blocks(component_specs, units, mcus_x, hmax, vmax, width, height)
        restart_every = restart_interval * _blocks_per_unit(component_specs) if restart_interval else 0
        staged = []
        for position, (cid, bx, by) in enumerate(blocks):
            if restart_every and position and position % restart_every == 0:
                predictors = {key: 0 for key in predictors}
            values = coefficients[cid][(bx, by)]
            if dc_split:
                staged.append((cid, values, None))
                if ah == 0:
                    value = values[0] >> al
                    dc_symbols.add(_category(value - predictors[cid]))
                    predictors[cid] = value
                continue
            diff = values[0] - predictors[cid]
            predictors[cid] = values[0]
            size = _category(diff)
            dc_symbols.add(size)
            runs = []
            index = 1
            zero_run = 0
            while index < 64:
                value = values[index]
                if value == 0:
                    zero_run += 1
                    index += 1
                    continue
                while zero_run > 15:
                    runs.append((0xF0, 0, 0))
                    ac_symbols.add(0xF0)
                    zero_run -= 16
                size_ac = _category(value)
                runs.append(((zero_run << 4) | size_ac, value, size_ac))
                ac_symbols.add((zero_run << 4) | size_ac)
                zero_run = 0
                index += 1
            if zero_run:
                runs.append((0x00, 0, 0))
                ac_symbols.add(0x00)
            staged.append((cid, values, (diff, size, runs)))
        if ss == 0 and se == 0 and ah > 0:
            dc_symbols.add(0)

        dc_counts, dc_order, dc_codes = _huffman_table(dc_symbols or {0})
        ac_counts, ac_order, ac_codes = _huffman_table(ac_symbols or {0})
        out += _dht(0, 0, dc_counts, dc_order)
        if se > 0:
            out += _dht(1, 0, ac_counts, ac_order)

        header = bytes([len(component_specs)])
        for cid, _h, _v in component_specs:
            header += bytes([cid, 0x00])
        header += bytes([ss, se, (ah << 4) | al])
        out += b"\xff\xda" + struct.pack(">H", len(header) + 2) + header

        writer = _Writer()
        predictors = {cid: 0 for cid, _h, _v in component_specs}
        unit_index = 0
        for position, (cid, values, rest) in enumerate(staged):
            if restart_interval and position and position % (restart_interval * _blocks_per_unit(component_specs)) == 0:
                writer.flush()
                out += writer.data
                out += bytes([0xFF, 0xD0 + (unit_index % 8)])
                writer = _Writer()
                predictors = {key: 0 for key in predictors}
                unit_index += 1
            if ss == 0 and se == 0:
                if ah == 0:
                    value = values[0] >> al
                    diff = value - predictors[cid]
                    predictors[cid] = value
                    size = _category(diff)
                    code, length = dc_codes[size]
                    writer.write(code, length)
                    if size:
                        writer.write(_amplitude(diff, size), size)
                else:
                    writer.write((values[0] >> al) & 1, 1)
                continue
            diff, size, runs = rest
            code, length = dc_codes[size]
            writer.write(code, length)
            if size:
                writer.write(_amplitude(diff, size), size)
            for symbol, value, size_ac in runs:
                code, length = ac_codes[symbol]
                writer.write(code, length)
                if size_ac:
                    writer.write(_amplitude(value, size_ac), size_ac)
        writer.flush()
        out += writer.data

    specs = [(cid, h, v) for cid, h, v, _p, _w, _h in planes]
    if progressive:
        emit_scan(specs, 0, 0, 0, 1, dc_split=True)
        emit_scan(specs, 0, 0, 1, 0, dc_split=True)
    else:
        emit_scan(specs, 0, 63, 0, 0)
    out += b"\xff\xd9"
    return bytes(out)


def _blocks_per_unit(component_specs):
    if len(component_specs) > 1:
        return sum(h * v for _cid, h, v in component_specs)
    return 1


def _scan_units(component_specs, mcus_x, mcus_y, width, height, hmax, vmax):
    if len(component_specs) > 1:
        return mcus_x * mcus_y
    _cid, h, v = component_specs[0]
    comp_w = (width * h + hmax - 1) // hmax
    comp_h = (height * v + vmax - 1) // vmax
    return ((comp_w + 7) // 8) * ((comp_h + 7) // 8)


def _scan_blocks(component_specs, units, mcus_x, hmax, vmax, width, height):
    blocks = []
    if len(component_specs) > 1:
        for unit in range(units):
            for cid, h, v in component_specs:
                for by in range(v):
                    for bx in range(h):
                        blocks.append((cid, (unit % mcus_x) * h + bx, (unit // mcus_x) * v + by))
        return blocks
    cid, h, v = component_specs[0]
    comp_w = (width * h + hmax - 1) // hmax
    per_row = (comp_w + 7) // 8
    for unit in range(units):
        blocks.append((cid, unit % per_row, unit // per_row))
    return blocks
