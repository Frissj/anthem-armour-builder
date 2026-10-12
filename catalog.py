"""
Per-javelin catalog (data/catalog_<javelin>.json), built the first time a javelin is used:
its armour sets, gear and the game data needed to assemble them.

  set part  appearances/<jav>_appearance_<set>_<part>_item.ebx -> appearance -> model ->
            mesh (EBX file references) -> MeshSet res
  textures  mesh EBX -> texture EBX references -> Texture res
  skeleton  the EX* rig whose rest pose best fits the frame mesh's skin weights
  pose      *_Idle_Forge, the Forge preview idle of the javelin's playerpreview bank
            (bind pose if none)
  gear      equipment/gear/<class>/**/<prefix>_*_model_mesh.ebx, rigged with
            <PREFIX>_skeleton, mounted on the javelin socket for its slot (bone + offset
            from <javelin>_sockets.ebx), clips <PREFIX>_* from whichever AssetBank holds them
Only this javelin's folders and animation banks are read.
"""
import json
import os
import re
import struct
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from antrig import ensure_rigs, cached  # noqa: E402
from config import DATA, bundle  # noqa: E402
from gamefs import fetch, fetch_tree, listing  # noqa: E402
from meshset import read_meshset  # noqa: E402

JAVELINS = {
    "exh_colossus": ("Colossus", "colossus"),
    "exf_interceptor": ("Interceptor", "interceptor"),
    "exm_lancer": ("Ranger", "lancer"),
    "exm_storm": ("Storm", "storm"),
}
PARTS = ("helm", "torso", "arms", "legs")

# Gear slot (from the gear's path in equipment/gear/<class>/) -> socket, as the game's
# exo/shared/sockets/*_gearsocketmap.ebx maps them (wrist slots -> Left/RightWristSocket,
# shoulder -> LeftShoulderSocket); each socket's bone and offset come from <javelin>_sockets.ebx
SLOT_SOCKETS = [
    (r"\\ultimate\\", "UltimateSocket"),
    (r"\\shoulder\\", "LeftShoulderSocket"),
    (r"shield", "LeftWristSocket"),
    (r"\\wrist\\", "RightWristSocket"),
]


def read_sockets(jav):
    """Socket name -> (bone, 16-float LinearTransform: right, up, forward, translation rows)
    from ebx/exo/shared/sockets/<javelin>_sockets.ebx. A socket record is ... u32 string
    offset of the bone, the LinearTransform, -1, u32 string offset of the socket name."""
    path = os.path.join(ebx_dir(), "exo", "shared", "sockets", jav.replace("_", "") + "_sockets.ebx")
    if not os.path.exists(path):
        return {}
    d = open(path, "rb").read()
    so, slen = struct.unpack_from("<I", d, 4)[0], struct.unpack_from("<I", d, 0x1C)[0]

    def text(off):
        return d[so + off:d.find(b"\0", so + off)].decode("latin1")

    out = {}
    for k in range(so + slen + 72, len(d) - 8, 4):
        tag, name = struct.unpack_from("<2I", d, k)
        bone = struct.unpack_from("<I", d, k - 68)[0]
        if tag == 0xFFFFFFFF and name < slen and bone < slen:
            out[text(name)] = (text(bone), list(struct.unpack_from("<16f", d, k - 64)))
    return out


def ebx_dir():
    return os.path.join(bundle(), "ebx")


def res_dir():
    return os.path.join(bundle(), "res")


def javelins():
    """Javelin key -> title, for the javelins present in the game."""
    return {k: t for k, (t, _) in JAVELINS.items()
            if listing("bundle/ebx/exo/%s/appearances/" % k)}


def extract_javelin(jav, gear_class, log=print):
    """Extract from the game install just what cataloguing this javelin reads: its EBX and
    mesh/texture headers, its gear's, the socket file and the render skeletons. Mesh and
    texture chunks follow when a build reads them."""
    headers = lambda n: n.endswith((".MeshSet", ".Texture"))  # noqa: E731
    fetch_tree("bundle/ebx/exo/%s/" % jav, log=log)
    fetch_tree("bundle/res/exo/%s/" % jav, headers, log)
    fetch_tree("bundle/ebx/equipment/gear/%s/" % gear_class, log=log)
    fetch_tree("bundle/res/equipment/gear/%s/" % gear_class, headers, log)
    fetch("bundle/ebx/exo/shared/sockets/%s_sockets.ebx" % jav.replace("_", ""))
    fetch_tree("bundle/ebx/animation/", lambda n: n.endswith("_skeleton.ebx"), log)


# ---------------------------------------------------------------- EBX references

def file_guid(path):
    with open(path, "rb") as f:
        return f.read(56)[40:56]


def index_ebx(root):
    """file GUID -> path for every EBX under root."""
    idx = {}
    for dp, _, fn in os.walk(root):
        for f in fn:
            if f.endswith(".ebx"):
                p = os.path.join(dp, f)
                idx[file_guid(p)] = p
    return idx


def refs(path, idx):
    """EBX files referenced by path (imports appear as file GUIDs in the partition)."""
    data = open(path, "rb").read()
    own = data[40:56]
    return [p for g, p in idx.items() if g != own and g in data]


def res_path(ebx_path, ext):
    p = os.path.join(res_dir(), os.path.relpath(ebx_path, ebx_dir()))[:-4] + ext
    return p if os.path.exists(p) else None


def textures_of(mesh_ebx, idx):
    """Texture sets referenced by a mesh: {set prefix: {kind: Texture res}}."""
    sets = {}
    for p in refs(mesh_ebx, idx):
        tex = res_path(p, ".Texture")
        if not tex:
            continue
        name = os.path.basename(p)[:-4]
        for kind, pat in (("mask", r"_(source_?mat_mask|sourcemat_mask|mat_mask)$"),
                          ("normal", r"_(source_norm|norm)$"),
                          ("aomo", r"_(aomo_mask|aomo)$"),
                          ("diffuse", r"_diff$")):
            m = re.search(pat, name)
            if m:
                sets.setdefault(name[:m.start()], {})[kind] = tex
                break
    return sets


def mesh_entry(mesh_ebx, idx):
    return {"name": os.path.basename(mesh_ebx)[:-4], "meshset": res_path(mesh_ebx, ".MeshSet"),
            "textures": textures_of(mesh_ebx, idx)}


# ---------------------------------------------------------------- armour sets

def follow_item(item, idx):
    """Item -> appearance(s) -> model(s) -> mesh EBX paths."""
    meshes = []
    appearances = [p for p in refs(item, idx)
                   if "_appearance_" in os.path.basename(p) and not p.endswith("_item.ebx")]
    for app in appearances:
        for p in refs(app, idx):
            b = os.path.basename(p)
            if "_slot_" in b:
                continue
            if b.endswith("_mesh.ebx") and res_path(p, ".MeshSet"):
                meshes.append(p)
            elif b.endswith("_model.ebx"):
                meshes += [q for q in refs(p, idx)
                           if q.endswith("_mesh.ebx") and res_path(q, ".MeshSet")]
    return sorted(set(meshes))


def armour_sets(jav, idx, log):
    folder = os.path.join(ebx_dir(), "exo", jav, "appearances")
    sets = {}
    for f in sorted(os.listdir(folder)):
        m = re.match(r"%s_appearance_(.+)_(%s)_item\.ebx$" % (jav, "|".join(PARTS)), f)
        if not m:
            continue
        name = m.group(1).replace(jav + "_", "")
        meshes = follow_item(os.path.join(folder, f), idx)
        if meshes:
            sets.setdefault(name, {})[m.group(2)] = [mesh_entry(p, idx) for p in meshes]
    log("  %d armour sets" % len(sets))
    return sets


# ---------------------------------------------------------------- skeleton

def render_bones(rig_name, rig):
    """Mesh bone order: the render skeleton EBX (animation/<p>/<p>_skeleton.ebx) lists its
    bones as strings in order. Mesh blend indices (through the section bone table) index
    this list, which is a reordered subset of the animation rig's joints (EXH: 183 of 201,
    same order only up to bone 47). Falls back to the rig's own joint order."""
    joints = [j for j, _ in rig["joints"]]
    p = os.path.join(ebx_dir(), "animation", rig_name.split("_")[0].lower(),
                     rig_name.lower() + ".ebx")
    if not os.path.exists(p):
        return joints
    seen, bones = set(joints), []
    for m in re.finditer(rb"[A-Za-z_][A-Za-z0-9_]{2,}\x00", open(p, "rb").read()):
        name = m.group()[:-1].decode()
        if name in seen and name not in bones:
            bones.append(name)
    return bones or joints


def quat_matrix(q):
    x, y, z, w = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def rest_positions(rig):
    by = {rig["dof_names"][k]: v for k, v in rig["defaults"].items() if k in rig["dof_names"]}
    world = []
    for name, parent in rig["joints"]:
        m = np.eye(4)
        m[:3, :3] = quat_matrix(by.get(name + ".q", (0, 0, 0, 1))[:4])
        m[:3, 3] = by.get(name + ".t", (0, 0, 0))[:3]
        world.append(world[parent] @ m if parent >= 0 else m)
    return np.array([w[:3, 3] for w in world])


def fit_skeleton(frame_meshset, rigs):
    """The EX* skeleton whose joints sit closest to the vertices they drive."""
    mesh = read_meshset(frame_meshset, skin=True)[0][0]
    verts, skin = np.array(mesh[1]), mesh[5]
    best = None
    for name, rig in rigs.items():
        if not re.match(r"EX[A-Za-z]*_skeleton$", name):
            continue
        joints = [j for j, _ in rig["joints"]]
        order = [joints.index(b) for b in render_bones(name, rig)]
        pos = rest_positions(rig)
        sums, counts = {}, {}
        for v, inf in enumerate(skin):
            if inf:
                b = max(inf, key=lambda x: x[1])[0]
                if b >= len(order):
                    break
                j = order[b]
                sums[j] = sums.get(j, 0) + verts[v]
                counts[j] = counts.get(j, 0) + 1
        else:
            err = np.mean([np.linalg.norm(sums[j] / counts[j] - pos[j])
                           for j in counts if counts[j] >= 20])
            if best is None or err < best[0]:
                best = (err, name)
    return best


# ---------------------------------------------------------------- clips

def clip_banks(jav, gear_class):
    """The AssetBanks that can hold this javelin's preview idle and gear clips (extracted)."""
    ant = "bundle/res/animations/antanimations/"
    short = jav.split("_")[1]

    def ours(n):
        d, f = n.rsplit("/", 1)
        return f.endswith(".AssetBank") and (gear_class in f or short in f
                                             or gear_class in d.lower())

    banks = [fetch(ant + "configurations/sharedbundles/globals_sharedbundle_win32_antstate.AssetBank")]
    for sub in ("exo/playerpreview/", "equipment/gear/%s/" % gear_class,
                "configurations/bundles/gearitemlist_bgc/"):
        banks += fetch_tree(ant + sub, ours)
    return banks


def scan_clip_names(banks, patterns):
    """Clip-like names matching any pattern -> AssetBanks containing them."""
    rx = re.compile(("|".join(patterns)).encode())
    found = {}
    for p in banks:
        for m in set(rx.findall(open(p, "rb").read())):
            found.setdefault(m.decode(), []).append(p)
    return found


def pick_bank(clips, banks_of):
    """The smallest bank holding the most of these clips."""
    score = {}
    for c in clips:
        for b in banks_of.get(c, []):
            score[b] = score.get(b, 0) + 1
    return max(score, key=lambda b: (score[b], -os.path.getsize(b))) if score else None


# ---------------------------------------------------------------- gear

def gear_meshes(gear_class):
    root = os.path.join(ebx_dir(), "equipment", "gear", gear_class)
    if not os.path.isdir(root):
        return {}, []
    idx = index_ebx(root)
    meshes = [p for p in sorted(idx.values())
              if p.endswith("_model_mesh.ebx") and res_path(p, ".MeshSet")]
    return idx, meshes


def gear_entries(idx, meshes, rigs, skeleton, banks_of, sockets):
    joints = {n for n, _ in rigs[skeleton]["joints"]}
    gear = {}
    for p in meshes:
        b = os.path.basename(p)
        prefix = b.split("_")[0].upper()
        rig = prefix + "_skeleton"
        socket = next((s for pat, s in SLOT_SOCKETS if re.search(pat, p, re.I)), None)
        if rig not in rigs or socket not in sockets or sockets[socket][0] not in joints:
            continue
        bone, offset = sockets[socket]
        label = b[len(prefix) + 1:-len("_model_mesh.ebx")]
        keyword = label.split("_")[-1].lower()
        clips = sorted(c for c in banks_of if c.startswith(prefix + "_"))
        # Rigs are shared (the mortar, minelayer and tesla all use GMOR): another piece's
        # clips name that piece, so keep this piece's own and the unnamed generic ones
        others = {os.path.basename(q).split("_")[-3].lower() for q in meshes
                  if os.path.basename(q).split("_")[0].upper() == prefix} - {keyword}
        clips = [c for c in clips if not any(o in c.lower() for o in others)]
        # Stowed pose: an idle for this piece, collapsed if it folds
        idles = [c for c in clips if "Idle" in c and "Aim" not in c and "Draw" not in c]
        idles.sort(key=lambda c: ("Collapsed" not in c, keyword not in c.lower(), len(c)))
        stowed = idles[0] if idles else None
        # The bank holding the stowed idle, then the most of this piece's clips
        bank = pick_bank([stowed] if stowed else clips, banks_of)
        gear[label] = {
            "mesh": mesh_entry(p, idx), "rig": rig, "socket": bone, "socket_offset": offset,
            "render_bones": render_bones(rig, rigs[rig]), "bank": bank,
            "clips": [c for c in clips if bank in banks_of[c]], "stowed": stowed,
        }
    return gear


# ---------------------------------------------------------------- build / load

def catalog_path(jav):
    return os.path.join(DATA, "catalog_%s.json" % jav)


def build(jav, log=print):
    title, gear_class = JAVELINS[jav]
    log("Cataloguing %s ..." % title)
    extract_javelin(jav, gear_class, log)
    idx =index_ebx(os.path.join(ebx_dir(), "exo", jav))
    frame = mesh_entry(os.path.join(ebx_dir(), "exo", jav, jav + "_base_model_mesh.ebx"), idx)
    gear_idx, gmeshes = gear_meshes(gear_class)
    prefixes = sorted({os.path.basename(p).split("_")[0].upper() for p in gmeshes})
    ensure_rigs([r"EX[A-Za-z]*_skeleton$"] + [r"%s_skeleton$" % p for p in prefixes], log)
    rigs = cached()
    err, skeleton = fit_skeleton(frame["meshset"], rigs)
    log("  skeleton %s (joints within %.3f m of their vertices)" % (skeleton, err))
    log("  scanning animation banks ...")
    banks_of = scan_clip_names(clip_banks(jav, gear_class),
                               [r"\b[A-Za-z]+(?:_[A-Za-z]+)?_Idle_Forge\b"]
                               + [r"\b%s_[A-Za-z0-9_]+" % p for p in prefixes])
    # The javelin's own preview idle (the Interceptor's is EXF_NOV_Idle_Forge), else <skeleton>_
    short = jav.split("_")[1]
    forges = [c for c in banks_of if c.endswith("_Idle_Forge")]
    forges.sort(key=lambda c: (not any("playerpreview" in b and short in b for b in banks_of[c]),
                               not c.startswith(skeleton.split("_")[0] + "_")))
    forge = forges[0] if forges else None
    gear = gear_entries(gear_idx, gmeshes, rigs, skeleton, banks_of, read_sockets(jav))
    log("  %d gear: %s" % (len(gear), ", ".join(sorted(gear))))
    cat = {
        "key": jav, "title": title, "skeleton": skeleton, "frame": frame,
        "render_bones": render_bones(skeleton, rigs[skeleton]),
        "pose": {"clip": forge, "bank": pick_bank([forge], banks_of)}
        if forge else None,
        "sets": armour_sets(jav, idx, log), "gear": gear,
    }
    log("  pose: %s" % (forge if cat["pose"] else "bind pose (no Forge idle found)"))
    os.makedirs(DATA, exist_ok=True)
    json.dump(cat, open(catalog_path(jav), "w"), indent=1)
    return cat


def load(jav, log=print):
    p = catalog_path(jav)
    return json.load(open(p)) if os.path.exists(p) else build(jav, log)


if __name__ == "__main__":
    for key in (sys.argv[1:] or javelins()):
        build(key)
