import logging
from struct import unpack
from typing import List, Any, BinaryIO, Optional, Tuple, TYPE_CHECKING

from anthemtool.cas.resource import Ebx, Resource, Chunk, File
from anthemtool.toc.entry import TocEntry
from anthemtool.util import ReadUtil

if TYPE_CHECKING:
    from anthemtool.toc.index import TocIndex


LOG = logging.getLogger(__name__)


class SBBundle:
    """
    Bundles contain multiple references to files that exist in CAS files:
     - Ebx
     - Resources
     - Chunks

    Each file reference provides at least the following data:
     - SHA1 sum
     - CAS file identifier (references a layout, package and cas index)
     - CAS offset
     - Compressed size

    Depending on the type of the file (Ebx, Resource, Chunk) some additional
    info is provided (Uncompressed Size, Name, ID, Content Type, etc).

    This class provides functionality to parse the .sb bundle and gives access to the files.
    """

    def __init__(self, index: 'TocIndex', bundle: BinaryIO, offset: int, name: str,
                 size: int, ref: int) -> None:
        """
        Initialize instance and start reading the sb at the given offset.
        """
        self.index = index
        self.name = name
        self.size = size
        self.ref = ref

        # Local entries
        self.files: List[File] = []
        self.ebx: List[Ebx] = []
        self.resources: List[Resource] = []
        self.chunks: List[Chunk] = []

        # Load bundle
        self.read(bundle, offset)

    def read(self, bundle: BinaryIO, offset: int) -> None:
        """
        Read from the given superbundle (.sb) file handle and parse it.
        During parsing some extra checks make sure the parsing is performed correctly.
        """
        LOG.debug("Reading bundle {} at offset 0x{:x}".format(self.name, offset))
        bundle.seek(offset)

        # Parse container data
        magic = unpack(">I", bundle.read(4))[0]
        if magic != 0x20:
            raise Exception("Expected TocIndex magic 0x20 but got 0x{:x}".format(magic))

        bundle.read(4)  # unknown
        bundle_len = unpack(">I", bundle.read(4))[0]
        bundle.read(4)  # count
        bundle.read(4)  # offset 1
        bundle.read(4)  # offset 2
        bundle.read(4)  # offset 3
        bundle.read(4)  # padding

        # Parse bundle data
        meta_size = unpack(">I", bundle.read(4))[0]
        meta_offset = bundle.tell()
        header = Header(unpack(">8I", bundle.read(32)))
        if header.magic != 0x9D798ED6:
            raise Exception("Invalid bundle magic")

        string_offset = meta_offset + header.string_offset

        # SHA1 hashes for all entries
        sha1_entries = [bundle.read(20) for i in range(header.total)]

        # Parse ebx entries
        self.ebx = [
            Ebx(
                sha1=sha1_entries[i],
                name=ReadUtil.read_string_rewind(
                    bundle, string_offset + unpack(">I", bundle.read(4))[0]
                ),
                orig_size=unpack(">I", bundle.read(4))[0],
            )
            for i in range(header.ebx)
        ]

        # Parse resource entries that have provide additional info
        self.resources = [
            Resource(
                sha1=sha1_entries[len(self.ebx) + i],
                name=ReadUtil.read_string_rewind(
                    bundle, string_offset + unpack(">I", bundle.read(4))[0]
                ),
                orig_size=unpack(">I", bundle.read(4))[0],
            )
            for i in range(header.resources)
        ]

        # Parse additional resource information
        for resource in self.resources:
            resource.content_type_id = unpack(">I", bundle.read(4))[0]

        for resource in self.resources:
            resource.meta = bundle.read(16)

        for resource in self.resources:
            resource.rid = unpack(">Q", bundle.read(8))[0]

        # Parse chunk entries that provide an ID instead of a name
        self.chunks = [
            Chunk(
                sha1=sha1_entries[len(self.ebx) + len(self.resources) + i],
                uid=bundle.read(16),
                range_start=unpack(">H", bundle.read(2))[0],
                logical_size=unpack(">H", bundle.read(2))[0],
                logical_offset=unpack(">I", bundle.read(4))[0],
            )
            for i in range(header.chunks)
        ]

        # Parse additional chunk meta data if available
        if header.chunks > 0:
            toc_entry = TocEntry()
            toc_entry.add_field(bundle)
            chunk_meta = toc_entry.get('chunkMeta')
        else:
            chunk_meta = []

        # Create a combined list of all entries
        self.files.extend(self.ebx)
        self.files.extend(self.resources)
        self.files.extend(self.chunks)

        # Skip parsing if we have no entries
        if header.total == 0:
            return

        # Parse the payload section that contains the CAS identifiers and offsets
        payload_start = meta_offset + meta_size
        bundle.seek(payload_start)
        payload = bundle.read(offset + bundle_len - payload_start)
        entries = self.parse_payload(payload)
        if entries is None:
            raise Exception(
                "Payload parsing error in {} ({}): no consistent CAS entry layout".format(
                    self.name, self.index
                )
            )

        for file, (entry_cas, addr, size) in zip(self.files, entries):
            file.offset = addr
            file.size = size
            file.cas = entry_cas
        bundle.seek(offset + bundle_len)

        # Parse more additional chunk meta data
        for chunk_idx, chunk in enumerate(self.chunks):
            chunk.h32 = chunk_meta[chunk_idx].h32
            chunk.first_mip = chunk_meta[chunk_idx].meta.get('firstMip')

        # Make sure we parsed until the end of the payload
        if bundle.tell() - offset != bundle_len:
            # Fix for an edge case that occurs once with the demo build
            if bundle_len - (bundle.tell() - offset) == 8:
                LOG.warning("Ignoring unexpected bytes: 0x%x", unpack(">Q", bundle.read(8))[0])
            else:
                raise Exception(
                    "Payload parsing error in {} ({}): parsed {} of {} bytes, check read_entry calls"
                    .format(self.name, self.index, bundle.tell() - offset, bundle_len)
                )

    def parse_payload(self, payload: bytes) -> Optional[List[Tuple[Any, int, int]]]:
        """
        Each entry is (offset, size), optionally prefixed with a new CAS identifier.
        The prefix cannot be told apart locally, so search for the layout that consumes the
        payload exactly, where every entry lies inside its CAS file at a valid block header.
        The usual reading (the read_entry heuristic) is tried first; backtracking is only
        needed where both readings are plausible.
        """
        n = len(self.files)
        end = len(payload)
        if end < 4:
            return None
        get_cas = self.index.package.get_cas

        def valid(cas: Any, addr: int, size: int) -> bool:
            return addr + size <= cas.file_size and cas.has_file_at(addr)

        def options(cas_id: int, pos: int) -> List[Tuple[int, int, int, int]]:
            # (cas_id, addr, size, next_pos) candidates, preferred first
            result = []
            if pos + 8 > end:
                return result
            value = unpack(">I", payload[pos:pos + 4])[0]
            plain = None
            cas = get_cas(cas_id)
            if cas:
                size = unpack(">I", payload[pos + 4:pos + 8])[0]
                if valid(cas, value, size):
                    plain = (cas_id, value, size, pos + 8)
            prefixed = None
            new_cas = get_cas(value)
            if new_cas and pos + 12 <= end:
                addr, size = unpack(">II", payload[pos + 4:pos + 12])
                if valid(new_cas, addr, size):
                    prefixed = (value, addr, size, pos + 12)
            # Same preference as read_entry: a CAS id that is not a file start in the
            # current CAS is taken as a prefix
            if prefixed and not (cas and cas.has_file_at(value)):
                result.append(prefixed)
                if plain:
                    result.append(plain)
            else:
                if plain:
                    result.append(plain)
                if prefixed:
                    result.append(prefixed)
            return result

        first = unpack(">I", payload[0:4])[0]
        # Iterative DFS: stack of (index, cas_id, pos, remaining options)
        chosen: List[Tuple[int, int, int, int]] = []
        stack = [options(first, 4)]
        failed = set()
        while stack:
            idx = len(chosen)
            if not stack[-1]:
                stack.pop()
                if chosen:
                    c = chosen.pop()
                    failed.add((len(chosen) + 1, c[0], c[3]))
                continue
            choice = stack[-1].pop(0)
            if (idx + 1, choice[0], choice[3]) in failed:
                continue
            chosen.append(choice)
            if len(chosen) == n:
                # 8 trailing bytes: edge case seen once with the demo build
                if choice[3] in (end, end - 8):
                    if choice[3] != end:
                        LOG.warning("Ignoring unexpected bytes in %s", self.name)
                    return [(get_cas(c[0]), c[1], c[2]) for c in chosen]
                chosen.pop()
                continue
            stack.append(options(choice[0], choice[3]))
        return None

    def read_entry(self, cas_id: int, bundle: BinaryIO) -> Tuple[int, int]:
        """
        An entry exists of an offset but might also be prefixed with a CAS identifier.
        This seems a bit weird and we cannot figure out when to expect one or the other.

        To work around this, we check if the next value validates as a valid CAS identifier,
        and if so, perform an additional check to get rid of false positives (offsets that
        also pass validation). The second check is not completely reliable but appears to
        work fine in practice.
        """
        addr = unpack(">I", bundle.read(4))[0]

        # Check if addr could be a valid cas identifier
        cas = self.index.package.get_cas(addr)
        if cas:
            # Check if the addr is a valid entry in the previous cas
            prev_cas = self.index.package.get_cas(cas_id)
            if prev_cas and not prev_cas.has_file_at(addr):
                return addr, unpack(">I", bundle.read(4))[0]

        return cas_id, addr

    def __repr__(self) -> str:
        return self.name if self.name else 'Unknown'


class Header:
    """
    Header for SBBundle.
    """
    def __init__(self, values: Tuple[Any, ...]):
        self.magic = values[0]
        self.total = values[1]
        self.ebx = values[2]
        self.resources = values[3]
        self.chunks = values[4]
        self.string_offset = values[5]
        self.chunk_meta_offset = values[6]
        self.chunk_meta_size = values[7]
