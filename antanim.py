"""
Decode Anthem ANT "Eclipse" animations (EclipseAnimationAsset, codec 'ECLP') to per-frame
local joint transforms. Pure Python so it also runs inside Bforartists.

Channels: quaternion channels first, then vector channels, matched in order to the clip's
ChannelToDofAsset DofIds; rigs.json (antrig.py) names each DOF ("Bone.q", "Bone.t", "Bone.s").
  key times   u8 frame per key (KeyTimeFormat 0) at Data[KeyTimes]
  vector key  VectorFormat 1: 3 x u16 LE at Data[KeyValues + 6k]: Min + Range * q / 65535
              VectorFormat 0: 3 x u8 at Data[KeyValues + 3k]: Min + Range * q / 255
              Min == Range marks a constant channel (value Min, no key data)
  quat key    "smallest three": 3 x u16 BE at Data[KeyValues + 6k], each (2q/max - 1)/sqrt2;
              the dropped (largest, >= 0) component's index is the low bits of words 0, 1
  quat, 1 key inline in (KeyTimes | KeyValues << 32): the top three 21-bit fields; dropped
              index in the word's low two bits
Verified: GCMS_Collapse's last keys equal GCMS_Idle_Collapsed's constants (vectors exact,
all 24 quaternions to |dot| > 0.9999998, mixing 48-bit and inline keys), and
EXH_Idle_Forge stands upright on the EXH_skeleton rest pose. (Reading the fields as
x, y, z with w >= 0 looked right on the gear, whose bones barely rotate, but folded the
character's limbs.)

FrameAnimationAsset (codec 'FRAM'): Data is raw floats per frame, quaternions (x, y, z, w)
first, then vectors padded to 4 floats, then floats; channels in the same order.
"""
import json
import math
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from assetbank import AssetBank, flatten  # noqa: E402
from config import DATA  # noqa: E402

RIGS = os.path.join(DATA, "rigs.json")

SQRT_HALF = math.sqrt(0.5)


def _smallest_three(fields, bits, dropped):
    """Three stored components in [-1/sqrt2, 1/sqrt2] plus the index of the dropped
    (largest, >= 0) component."""
    m = (1 << bits) - 1
    vals = [(2.0 * f / m - 1.0) * SQRT_HALF for f in fields]
    q = vals[:dropped] + [math.sqrt(max(0.0, 1.0 - sum(v * v for v in vals)))] + vals[dropped:]
    return tuple(q)


def _quat48(data, off):
    # Dropped index: low bits of the first two words
    f = struct.unpack_from(">3H", data, off)
    return _smallest_three(f, 16, ((f[0] & 1) << 1) | (f[1] & 1))


def _quat_inline(lo, hi):
    # Dropped index: the low two bits of the 64-bit word
    u = lo | (hi << 32)
    m = (1 << 21) - 1
    return _smallest_three([(u >> (43 - 21 * j)) & m for j in range(3)], 21, u & 3)


def _lerp(a, b, t):
    return tuple(x + (y - x) * t for x, y in zip(a, b))


def _normalize(q):
    n = math.sqrt(sum(x * x for x in q)) or 1.0
    return tuple(x / n for x in q)


def _nlerp(a, b, t):
    if sum(x * y for x, y in zip(a, b)) < 0:
        b = tuple(-y for y in b)
    return _normalize(_lerp(a, b, t))


def _sample(times, values, frame, interp):
    if frame <= times[0]:
        return values[0]
    for k in range(1, len(times)):
        if frame <= times[k]:
            t = (frame - times[k - 1]) / float(times[k] - times[k - 1] or 1)
            return interp(values[k - 1], values[k], t)
    return values[-1]


class Clip:
    def __init__(self, bank, name, rig):
        ctrl = flatten(bank.find(name))
        anim = flatten(bank.by_key[ctrl["Anim"]])
        if anim["__type"] not in ("EclipseAnimationAsset", "FrameAnimationAsset"):
            raise Exception("%s uses %s, not supported" % (name, anim["__type"]))
        key = anim["ChannelToDofAsset"]
        if key in bank.by_key:
            ids = flatten(bank.by_key[key])["DofIds"]
        else:  # shared channel map from the global bank (antrig.py)
            ids = _global_channel_maps()[str(key)]
        names = [rig["dof_names"].get(str(i), "dof_%08x" % i) for i in ids]
        self.name = name
        self.fps = ctrl["FPS"]
        self.frames = anim["EndFrame"] + 1
        self.additive = anim["Additive"]
        self.channels = []  # (dof name, key frames, values, interp)
        if anim["__type"] == "FrameAnimationAsset":
            self._read_frames(anim, names)
            return
        data = bytes(anim["Data"])
        vec_size = {0: 3, 1: 6}[anim["VectorFormat"]]
        vec_fmt, vec_max = {0: ("<3B", 255.0), 1: ("<3H", 65535.0)}[anim["VectorFormat"]]
        quats, vecs = anim["QuaternionChannels"], anim["VectorChannels"]
        for i, c in enumerate(quats):
            n = c["KeyCount"]
            if n == 1:
                times, vals = [0], [_quat_inline(c["KeyTimes"], c["KeyValues"])]
            else:
                times = list(data[c["KeyTimes"]:c["KeyTimes"] + n])
                vals = [_quat48(data, c["KeyValues"] + 6 * k) for k in range(n)]
            self.channels.append((names[i], times, vals, _nlerp))
        for i, c in enumerate(vecs):
            lo, rng = c["Min"], c["Range"]
            if tuple(lo) == tuple(rng) or not data:
                times, vals = [0], [tuple(lo)]
            else:
                n = c["KeyCount"]
                times = list(data[c["KeyTimes"]:c["KeyTimes"] + n])
                vals = [tuple(lo[j] + rng[j] * q[j] / vec_max for j in range(3))
                        for q in (struct.unpack_from(vec_fmt, data,
                                                     c["KeyValues"] + vec_size * k)
                                  for k in range(n))]
            self.channels.append((names[len(quats) + i], times, vals, _lerp))

    def _read_frames(self, anim, names):
        nq, nv, nf = anim["QuatCount"], anim["Vec3Count"], anim["FloatCount"]
        data = anim["Data"]
        per_frame = 4 * nq + 4 * nv + nf
        times = list(range(self.frames))
        frames = [data[f * per_frame:(f + 1) * per_frame] for f in range(self.frames)]
        for i in range(nq):
            vals = [_normalize(fr[4 * i:4 * i + 4]) for fr in frames]
            self.channels.append((names[i], times, vals, _nlerp))
        for i in range(nv):
            o = 4 * nq + 4 * i
            self.channels.append((names[nq + i], times, [tuple(fr[o:o + 3]) for fr in frames],
                                  _lerp))

    def pose(self, frame):
        """{dof name: value} at a frame."""
        return {name: _sample(times, vals, frame, interp)
                for name, times, vals, interp in self.channels}


_RIGS = {}


def _rigs():
    if not _RIGS:
        _RIGS.update(json.load(open(RIGS)))
    return _RIGS


def _global_channel_maps():
    return _rigs()["channel_maps"]


def load_rig(name):
    return _rigs()[name]


def rest_locals(rig):
    """Per joint (t, q, s) from the rig's default DOF values."""
    by_name = {rig["dof_names"][k]: v for k, v in rig["defaults"].items()
               if k in rig["dof_names"]}
    out = []
    for joint, _parent in rig["joints"]:
        t = tuple(by_name.get(joint + ".t", (0, 0, 0))[:3])
        q = tuple(by_name.get(joint + ".q", (0, 0, 0, 1))[:4])
        s = tuple(by_name.get(joint + ".s", (1, 1, 1))[:3])
        out.append((t, q, s))
    return out


def posed_locals(rig, pose):
    """Rest locals overridden by a clip pose."""
    out = []
    for (joint, _parent), (t, q, s) in zip(rig["joints"], rest_locals(rig)):
        out.append((tuple(pose.get(joint + ".t", t))[:3], tuple(pose.get(joint + ".q", q))[:4],
                    tuple(pose.get(joint + ".s", s))[:3]))
    return out


if __name__ == "__main__":
    bank = AssetBank(sys.argv[1])
    rig = load_rig(sys.argv[2])
    clip = Clip(bank, sys.argv[3], rig)
    print(clip.name, clip.fps, "fps", clip.frames, "frames", len(clip.channels), "channels")
    pose = clip.pose(clip.frames - 1)
    for k in sorted(pose)[:12]:
        print("  ", k, tuple(round(x, 4) for x in pose[k]))
