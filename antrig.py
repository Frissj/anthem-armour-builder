"""
Extract ANT rigs (skeleton joints, DOF names/ids, rest values) from the global AssetBank
into data/rigs.json, on demand: ensure_rigs(names) parses the bank only when a requested
rig is not cached yet.

A RigAsset lists DofIds per DOF set (RigDofSets / DofSetIdIndices); LayoutAsset slots give
the DOF names in the same order ("Bone.q" rotation, "Bone.t" translation, "Bone.s" scale).
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from assetbank import AssetBank, flatten  # noqa: E402
from config import DATA  # noqa: E402
from gamefs import fetch  # noqa: E402

OUT = os.path.join(DATA, "rigs.json")


def globals_bank():
    return fetch("bundle/res/animations/antanimations/configurations/sharedbundles/"
                 "globals_sharedbundle_win32_antstate.AssetBank")


def extract(g, rig):
    skel = flatten(g.by_key[rig["Skeleton"]])
    names = {}
    n = len(rig["DofIds"])
    # Clamp: a few rigs carry out-of-range set indices, which made this loop run forever
    starts = [min(s, n) for s in rig["DofSetIdIndices"]] + [n]
    for i, key in enumerate(rig["RigDofSets"][:len(starts) - 1]):
        if key not in g.by_key:
            continue
        slots = flatten(g.by_key[key]).get("Slots", [])
        for j in range(starts[i], min(starts[i + 1], starts[i] + len(slots))):
            names[rig["DofIds"][j]] = slots[j - starts[i]]["Name"]
    defaults = {}
    for v in rig["DefaultVector3Values"] + rig["DefaultVector4Values"]:
        defaults[v["DofId"]] = list(v["Value"])
    return {
        "joints": [(j["JointName"], j["ParentIndex"]) for j in skel["Joints"]],
        "dof_names": {str(k): v for k, v in names.items()},
        "defaults": {str(k): v for k, v in defaults.items()},
    }


def cached():
    return json.load(open(OUT)) if os.path.exists(OUT) else {}


def ensure_rigs(patterns, log=print):
    """Make sure every rig whose name matches one of the regex patterns is cached.
    Returns the cached rig names."""
    rigs = cached()
    have = set(rigs)
    if rigs.get("_patterns_done", []) and all(p in rigs["_patterns_done"] for p in patterns):
        return have
    log("Reading skeletons from the global animation bank ...")
    g = AssetBank(globals_bank())
    for o in g.objects:
        if o["__type"] != "RigAsset" or not o.get("__name") or o["__name"] in rigs:
            continue
        if not any(re.match(p, o["__name"]) for p in patterns):
            continue
        rig = flatten(o)
        if rig.get("Skeleton") in g.by_key:
            rigs[rig["__name"]] = extract(g, rig)
    if "channel_maps" not in rigs:
        # Clips in other banks may use channel maps that only live in the global bank
        rigs["channel_maps"] = {str(o["__key"]): flatten(o)["DofIds"] for o in g.objects
                                if o["__type"] == "ChannelToDofAsset"}
    rigs["_patterns_done"] = sorted(set(rigs.get("_patterns_done", [])) | set(patterns))
    os.makedirs(DATA, exist_ok=True)
    json.dump(rigs, open(OUT, "w"))
    return set(rigs)


if __name__ == "__main__":
    print(sorted(n for n in ensure_rigs(sys.argv[1:] or [r".*_skeleton$"])
                 if not n.startswith("_") and n != "channel_maps"))
