"""Anthem (Frostbite) .Texture -> PNG, decoding the pixel chunk."""
import os
import struct
import uuid

import texture2ddecoder as td
from PIL import Image

from gamefs import find_chunks

# Frostbite format id -> (decoder, bytes per 4x4 block or per pixel for raw)
DECODERS = {
    "bc1": (td.decode_bc1, 8),
    "bc4": (td.decode_bc4, 8),
    "bc3": (td.decode_bc3, 16),
    "bc5": (td.decode_bc5, 16),
    "bc7": (td.decode_bc7, 16),
}


def find_chunk(guid):
    # The TOC and bundle copies can differ: the larger one has the top mips
    found = find_chunks(str(uuid.UUID(bytes_le=guid)))
    return max(found, key=os.path.getsize) if found else None


def read_header(path):
    b = open(path, "rb").read()
    fmt = struct.unpack_from("<I", b, 0x0C)[0]
    w, h = struct.unpack_from("<HH", b, 0x16)
    mips = b[0x1E]
    sizes = struct.unpack_from("<%dI" % mips, b, 0x30)
    return fmt, w, h, mips, sizes, find_chunk(b[0x20:0x30])


def decode(path, codec, mip=0):
    fmt, w, h, mips, sizes, chunk = read_header(path)
    data = open(chunk, "rb").read()
    # A short chunk holds only the low mips (the streamed top mips live elsewhere)
    skip = sum(sizes[:mip])
    if len(data) < sum(sizes):
        drop = len(sizes) - [sum(sizes[i:]) for i in range(len(sizes))].index(len(data))
        skip -= sum(sizes[:drop])
    w, h = max(1, w >> mip), max(1, h >> mip)
    blob = data[skip:skip + sizes[mip]]
    if codec == "rgba8":
        return Image.frombytes("RGBA", (w, h), blob)
    fn, _ = DECODERS[codec]
    return Image.frombytes("RGBA", (w, h), fn(blob, w, h), "raw", "BGRA")


if __name__ == "__main__":
    import sys
    path, codec, mip, out = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
    decode(path, codec, mip).save(out)
