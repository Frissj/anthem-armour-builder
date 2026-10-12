"""
Anthem (Frostbite ANT) .AssetBank reader.

Layout: big-endian version + header size, then sections "GD.xxxx" + endian char + size:
  GD.STRM  stream header
  GD.REFL  type library ("banks": class name + fields with type, offset, size)
  GD.DATA  one object instance each, laid out per its bank
Section/bank layout follows FrostyToolsuite's AnimationEditorPlugin (1.0.7), which only
reads the type library; the object decoding below is our own.
"""
import struct

# Field type ids (FrostyToolsuite BankType)
TYPES = ["Invalid", "Boolean", "Int8", "UInt8", "Int16", "UInt16", "Int32", "UInt32",
         "Int64", "UInt64", "Float", "Vector2", "Vector3", "Vector4", "Quaternion",
         "Matrix", "Guid", "String", "Reference", "Double"]


class Field:
    pass


class Bank:
    pass


def read_refl(b, pos, end):
    """Parse the type library. Returns {type hash: Bank}."""
    size = struct.unpack_from("<I", b, pos)[0]
    start = pos + 4
    count = struct.unpack_from("<Q", b, start)[0]
    offsets = struct.unpack_from("<%dQ" % count, b, start + 8)
    banks_at = {}
    for off in offsets:
        banks_at[off] = Bank()
    for off, bank in banks_at.items():
        p = start + off
        (mn, mx, bank.size, bank.align, _unk, name_size) = struct.unpack_from("<iiiiIi", b, p)
        p += 24 + 4  # two bools + ushort
        bank.type_hash = struct.unpack_from("<I", b, p)[0]
        p += 4
        bank.fields = []
        for i in range(mx - mn + 1):
            f = Field()
            f.type, f.size, f.offset, f.name_off = struct.unpack_from("<IiiI", b, p)
            f.is_array = struct.unpack_from("<H", b, p + 18)[0] != 0
            ref = struct.unpack_from("<Q", b, p + 24)[0]
            f.bank_off = ref
            p += 32
            bank.fields.append(f)
        names = b[p:p + name_size]

        def name(o):
            return names[o:names.index(b"\0", o)].decode("ascii", "replace")
        bank.name = name(1)
        for f in bank.fields:
            f.name = name(f.name_off)
            f.type_name = TYPES[f.type] if f.type < len(TYPES) else "0x%x" % f.type
        bank.offset = off
    for bank in banks_at.values():
        for f in bank.fields:
            f.bank = banks_at.get(f.bank_off)
    return {bank.type_hash: bank for bank in banks_at.values()}


def sections(b):
    """Yield (format, endian, payload start, payload end)."""
    pos = struct.unpack_from(">I", b, 4)[0] + 4
    while pos + 12 <= len(b) and b[pos:pos + 3] == b"GD.":
        fmt = b[pos + 3:pos + 7].decode()
        endian = b[pos + 7:pos + 8].decode()
        size = struct.unpack_from("<I", b, pos + 8)[0]
        if fmt == "STRM":
            # STRM wraps the rest of the file; step inside it
            yield fmt, endian, pos + 12, pos + size
            pos += 12 + 4
            continue
        yield fmt, endian, pos + 12, pos + size
        pos += size


PRIM = {
    "Boolean": ("<?", 1), "Int8": ("<b", 1), "UInt8": ("<B", 1), "Int16": ("<h", 2),
    "UInt16": ("<H", 2), "Int32": ("<i", 4), "UInt32": ("<I", 4), "Int64": ("<q", 8),
    "UInt64": ("<Q", 8), "Float": ("<f", 4), "Double": ("<d", 8),
}
VEC = {"Vector2": 2, "Vector3": 3, "Vector4": 4, "Quaternion": 4}
KEY_HASH = 0x23

# Object header (32 bytes): magic u32, u64, u32, type hash u32, u64, data offset u32.
# All offsets inside a DATA record are relative to the record start (after its size field).


class AssetBank:
    def __init__(self, path):
        self.b = open(path, "rb").read()
        self.banks = {}
        self.records = []
        for fmt, endian, start, end in sections(self.b):
            if fmt == "REFL":
                self.banks = read_refl(self.b, start, end)
            elif fmt == "DATA":
                self.records.append(start + 4)
        self.objects = [self.read_object(r, r) for r in self.records]
        self.by_key = {}
        for o in self.objects:
            k = o.get("__key")
            if k:
                self.by_key[k] = o

    def read_object(self, base, hdr):
        type_hash = struct.unpack_from("<I", self.b, hdr + 16)[0]
        data_off = struct.unpack_from("<I", self.b, hdr + 28)[0]
        bank = self.banks.get(type_hash)
        obj = {"__type": bank.name if bank else "0x%08x" % type_hash}
        if bank:
            self.read_fields(base, hdr + data_off, bank, obj)
        return obj

    def read_fields(self, base, pos, bank, obj):
        for f in bank.fields:
            if not f.name:
                continue
            if f.is_array:
                count, _cap, off = struct.unpack_from("<IIQ", self.b, pos + f.offset)
                end = base + off + count * self.elem_size(f)
                if off and not (base < base + off and end <= len(self.b)):
                    obj[f.name] = ("bad array", count, off, pos + f.offset)
                    continue
                obj[f.name] = [self.read_value(base, base + off + i * self.elem_size(f), f)
                               for i in range(count)] if off else []
            else:
                obj[f.name] = self.read_value(base, pos + f.offset, f)

    def elem_size(self, f):
        if f.type_name in PRIM:
            return PRIM[f.type_name][1]
        if f.type_name in VEC:
            return 16
        if f.type == KEY_HASH:
            return 8
        if not f.bank:
            return f.size
        # Bank sizes are unpadded; array elements are padded to the bank's alignment
        align = max(1, f.bank.align)
        return (f.bank.size + align - 1) // align * align

    def read_value(self, base, pos, f):
        t = f.type_name
        if t in PRIM:
            return struct.unpack_from(PRIM[t][0], self.b, pos)[0]
        if t in VEC:
            return struct.unpack_from("<%df" % VEC[t], self.b, pos)
        if t == "String":
            n, _cap, off = struct.unpack_from("<IIQ", self.b, pos)
            return self.b[base + off:base + off + n].split(b"\0")[0].decode("ascii", "replace") if off else ""
        if f.type == KEY_HASH:
            return struct.unpack_from("<Q", self.b, pos)[0]
        if t == "Reference":
            off = struct.unpack_from("<Q", self.b, pos)[0] & 0x0FFFFFFFFFFFFFFF
            return self.read_object(base, base + off) if off else None
        if t == "Matrix":
            return struct.unpack_from("<16f", self.b, pos)
        if t == "Guid":
            return self.b[pos:pos + 16].hex()
        if f.bank:  # embedded struct
            sub = {"__type": f.bank.name}
            self.read_fields(base, pos, f.bank, sub)
            return sub
        return self.b[pos:pos + f.size].hex()

    def find(self, name):
        for o in self.objects:
            if o.get("__name") == name:
                return o
        return None


def flatten(obj):
    """Merge an object's __base chain into one dict."""
    out = {}
    while obj:
        for k, v in obj.items():
            if k != "__base":
                out.setdefault(k, v)
        obj = obj.get("__base")
    return out


if __name__ == "__main__":
    import sys
    b = open(sys.argv[1], "rb").read()
    secs = list(sections(b))
    print([(s[0], hex(s[2]), s[3] - s[2]) for s in secs[:5]], len(secs))
    refl = [s for s in secs if s[0] == "REFL"][0]
    banks = read_refl(b, refl[2], refl[3])
    for bank in sorted(banks.values(), key=lambda x: x.name):
        print("%-40s %08x size=%d" % (bank.name, bank.type_hash, bank.size))
        if len(sys.argv) > 2 and any(k in bank.name for k in sys.argv[2:]):
            for f in bank.fields:
                print("    %-30s %-10s off=%-4d size=%-4d arr=%d sub=%s" % (
                    f.name, f.type_name, f.offset, f.size, f.is_array,
                    f.bank.name if f.bank else ""))
