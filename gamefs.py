"""
Game files straight from the Anthem install, extracted only when asked for.

The first use reads the install's layouts, TOCs and superbundles once (anthemtool, bundled
here) into data/game_index.sqlite: every ebx/res/chunk path -> CAS archive, offset, sizes
(Patch before Data, as anthemtool's export). After that only the files a javelin needs are
decompressed into data/game/, laid out like an anthemtool export (bundle/ebx, bundle/res,
bundle/chunks, chunks), each file once.
"""
import os
import sqlite3

from config import DATA, GAME_FILES as ROOT, load

INDEX = os.path.join(DATA, "game_index.sqlite")

_db = None
_writer = None
_cas = {}


def build_index(log=print):
    from anthemtool.game import FrostbiteGame
    log("Indexing the game install (once, a few minutes) ...")
    game = FrostbiteGame(load()["game"])
    cas_ids, rows = {}, []

    def add(rel, item):
        if item.cas and item.offset is not None and item.size is not None:
            cid = cas_ids.setdefault(item.cas.path, len(cas_ids))
            rows.append((rel.lower(), rel, cid, item.offset, item.size, item.orig_size))

    for layout in (game.layout_patch, game.layout_data):
        for package in layout.packages.values():
            for superbundles in (package.splitsuperbundles, package.superbundles):
                for index in superbundles.values():
                    if not index:
                        continue
                    for b in index.bundles:
                        for e in b.ebx:
                            add("bundle/ebx/" + e.filename, e)
                        for r in b.resources:
                            add("bundle/res/" + r.filename, r)
                        for c in b.chunks:
                            add("bundle/chunks/" + c.filename, c)
                    for r in index.resources:
                        add("chunks/" + r.filename, r)
    os.makedirs(DATA, exist_ok=True)
    tmp = INDEX + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    con.execute("CREATE TABLE cas (id INTEGER PRIMARY KEY, path TEXT)")
    con.execute("CREATE TABLE files (key TEXT PRIMARY KEY, name TEXT, cas INTEGER, "
                "offset INTEGER, size INTEGER, osize INTEGER)")
    con.executemany("INSERT INTO cas VALUES (?, ?)", [(i, p) for p, i in cas_ids.items()])
    # First entry wins: the Patch layout overrides Data, like the export's skip-existing
    con.executemany("INSERT OR IGNORE INTO files VALUES (?, ?, ?, ?, ?, ?)", rows)
    con.commit()
    con.close()
    os.replace(tmp, INDEX)
    log("  %d game files indexed" % len(rows))


def db(log=print):
    global _db
    if _db is None:
        if not os.path.exists(INDEX):
            build_index(log)
        _db = sqlite3.connect(INDEX, check_same_thread=False)
    return _db


def local(rel):
    return os.path.join(ROOT, *rel.split("/"))


class _Cas:
    def __init__(self, path):
        self.path = path
        self.handle = open(path, "rb")


def _extract(name, cas, offset, size, osize):
    global _writer
    if _writer is None:
        from anthemtool.io.providers.null import NullDecompressor
        from anthemtool.io.providers.oodle import OodleDecompressor
        from anthemtool.io.writer import CasWriter
        _writer = CasWriter({"null": NullDecompressor(), "oodle": OodleDecompressor(
            os.path.join(load()["game"], "oo2core_7_win64.dll"))})
    if cas not in _cas:
        _cas[cas] = _Cas(db().execute("SELECT path FROM cas WHERE id = ?", (cas,)).fetchone()[0])
    out = local(name)
    _writer.write(_cas[cas], offset, out + ".part", size, osize)
    os.replace(out + ".part", out)


def fetch(rel):
    """Local path of a game file (path as in an export, '/'-separated), extracting it
    on first use; None if the game has no such file."""
    p = local(rel)
    if os.path.exists(p):
        return p
    row = db().execute("SELECT name, cas, offset, size, osize FROM files WHERE key = ?",
                       (rel.lower(),)).fetchone()
    if not row:
        return None
    _extract(*row)
    return p


def listing(prefix):
    """Paths of the game files under a folder prefix ('/'-separated, ending in '/')."""
    p = prefix.lower()
    return [r[0] for r in db().execute(
        "SELECT name FROM files WHERE key >= ? AND key < ?", (p, p + "￿"))]


def fetch_tree(prefix, keep=None, log=None):
    """Extract the files under prefix (those keep(name) accepts); returns their local paths."""
    names = [n for n in listing(prefix) if keep is None or keep(n)]
    missing = [n for n in names if not os.path.exists(local(n))]
    if missing and log:
        log("  extracting %d files from %s" % (len(missing), prefix))
    for n in missing:
        fetch(n)
    return [local(n) for n in names]


def find_chunks(guid):
    """Local copies of a chunk (uuid string): the TOC chunk and/or the bundle chunk."""
    return [p for p in (fetch("chunks/%s.chunk" % guid), fetch("bundle/chunks/%s.chunk" % guid))
            if p]
