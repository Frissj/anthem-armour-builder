"""Anthem (Frostbite) MeshSet reader: LOD -> sections -> vertices/indices from the chunk."""
import os
import struct
import uuid

from gamefs import find_chunks

SECTION_SIZE = 0xE0

# Vertex element usages (Frostbite)
POS, NORMAL, TEX0, TEX1 = 0x01, 0x06, 0x21, 0x22
# Skinning: 4 x u16 bone indices and 4 x u8 weights, a second set for 8 influences
BLEND_INDICES, BLEND_INDICES2, BLEND_WEIGHTS, BLEND_WEIGHTS2 = 0x02, 0x03, 0x04, 0x05


def half(h):
    return struct.unpack("<e", struct.pack("<H", h))[0]


def cstr(b, off):
    return b[off:b.index(b"\0", off)].decode("ascii", "replace")


def find_chunk(guid):
    found = find_chunks(str(uuid.UUID(bytes_le=guid)))
    return found[0] if found else None


class Section:
    pass


def read_lod(b, lod_off):
    count, sec_off = struct.unpack_from("<IQ", b, lod_off + 0x0C)
    size_a, size_b = struct.unpack_from("<II", b, lod_off + 0x5C)
    guid = b[lod_off + 0x64:lod_off + 0x74]
    sections = []
    for i in range(count):
        o = sec_off + i * SECTION_SIZE
        s = Section()
        s.name = cstr(b, struct.unpack_from("<Q", b, o + 0x08)[0])
        s.material = struct.unpack_from("<I", b, o + 0x10)[0]
        s.prims, s.start_index, s.vertex_offset, s.vertex_count = struct.unpack_from("<4I", b, o + 0x18)
        s.stride = b[o + 0x28]
        # Section bone table: local blend index -> skeleton joint index
        bone_count = struct.unpack_from("<I", b, o + 0x2C)[0]
        bone_off = struct.unpack_from("<Q", b, o + 0x30)[0]
        s.bones = (list(struct.unpack_from("<%dH" % bone_count, b, bone_off))
                   if bone_off and bone_off + 2 * bone_count <= len(b) else [])
        usages = [(b[o + 0x40 + 4 * k], b[o + 0x41 + 4 * k]) for k in range(16)]
        sizes = struct.unpack_from("<16H", b, o + 0x80)
        s.elements, off = {}, 0
        for (usage, fmt), size in zip(usages, sizes):
            if usage == 0 or size == 0:
                break
            s.elements[usage] = (fmt, off, size)
            off += size
        sections.append(s)
    return guid, size_a, size_b, sections


def read_skin(data, s, stream):
    """Per vertex [(joint index, weight)] from the planar blend streams."""
    n = s.vertex_count
    inf = [[] for _ in range(n)]
    for iu, wu in ((BLEND_INDICES, BLEND_WEIGHTS), (BLEND_INDICES2, BLEND_WEIGHTS2)):
        if iu not in stream or wu not in stream:
            continue
        idx = struct.unpack_from("<%dH" % (4 * n), data, stream[iu])
        wts = data[stream[wu]:stream[wu] + 4 * n]
        for v in range(n):
            for k in range(4):
                w = wts[4 * v + k]
                if w:
                    local = idx[4 * v + k]
                    inf[v].append((s.bones[local] if local < len(s.bones) else local, w / 255.0))
    return inf


def read_meshset(path, lod=0, skin=False):
    """Meshes as (name, verts, faces, uvs, normals), plus per-vertex skin when skin=True."""
    b = open(path, "rb").read()
    lod_offsets = struct.unpack_from("<6Q", b, 0x20)
    guid, size_a, size_b, sections = read_lod(b, lod_offsets[lod])
    data = open(find_chunk(guid), "rb").read()
    # The larger of the two sizes is the vertex buffer; indices follow it
    vb_size = max(size_a, size_b)
    ib_size = min(size_a, size_b)
    meshes = []
    for s in sections:
        # Unnamed sections are shadow/depth-only copies of the named ones
        if not s.name or s.prims == 0 or s.vertex_count == 0 or POS not in s.elements:
            continue
        n = s.vertex_count
        # Planar layout: each element is its own stream of n values, in element order
        stream = {usage: s.vertex_offset + off * n for usage, (_, off, _) in s.elements.items()}
        pos_fmt = s.elements[POS][0]
        if pos_fmt == 3:  # float3 (armour, frame)
            verts = list(struct.iter_unpack("<3f", data[stream[POS]:stream[POS] + 12 * n]))
        elif pos_fmt == 8:  # half4 (gear)
            raw = struct.unpack_from("<%de" % (4 * n), data, stream[POS])
            verts = [raw[4 * i:4 * i + 3] for i in range(n)]
        else:
            raise Exception("Unsupported position format %d in %s" % (pos_fmt, path))
        uvs, normals = [], []
        if TEX0 in stream:
            raw = struct.unpack_from("<%de" % (2 * n), data, stream[TEX0])
            uvs = [(raw[2 * i], 1.0 - raw[2 * i + 1]) for i in range(n)]
        if NORMAL in stream:
            raw = struct.unpack_from("<%de" % (4 * n), data, stream[NORMAL])
            normals = [raw[4 * i:4 * i + 3] for i in range(n)]
        idx_size = 4 if s.vertex_count > 0xFFFF else 2
        ifmt = "<%d%s" % (s.prims * 3, "I" if idx_size == 4 else "H")
        idx = struct.unpack_from(ifmt, data, vb_size + s.start_index * idx_size)
        faces = [tuple(idx[i:i + 3]) for i in range(0, len(idx), 3)]
        mesh = (s.name, verts, faces, uvs, normals)
        meshes.append(mesh + (read_skin(data, s, stream),) if skin else mesh)
    return meshes, (vb_size, ib_size, len(data)), sections


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        meshes, sizes, sections = read_meshset(p)
        print(os.path.basename(p), "vb/ib/chunk", sizes)
        for s in sections:
            print("  %-15s mat=%d prims=%d start=%d voff=%#x vcount=%d stride=%d elems=%s" % (
                s.name, s.material, s.prims, s.start_index, s.vertex_offset, s.vertex_count,
                s.stride, {hex(k): v for k, v in s.elements.items()}))
        for name, verts, faces, uvs, normals in meshes:
            mx = max(max(f) for f in faces)
            xs = [v[0] for v in verts]; ys = [v[1] for v in verts]; zs = [v[2] for v in verts]
            print("  mesh %-15s v=%d f=%d maxidx=%d bbox x[%.2f,%.2f] y[%.2f,%.2f] z[%.2f,%.2f]" % (
                name, len(verts), len(faces), mx, min(xs), max(xs), min(ys), max(ys), min(zs), max(zs)))
