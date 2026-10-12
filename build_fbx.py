"""
Bforartists/Blender (background): build a javelin from a job file and export an FBX.

  bforartists.exe --background --factory-startup --python build_fbx.py -- job.json

One armature: the javelin skeleton (rigs.json, bind pose) with each gear rig merged under
its socket joint (bones prefixed "<RIG>_"). Armour, frame and gear meshes are skinned to it
with the game's weights. Actions:
  <name>_Forge      javelin Forge idle (or bind pose) with every gear in its stowed clip
  <RIG>_<clip>      each gear clip, keyed on that gear's bones only
Coordinates are converted Frostbite (Y-up, facing +Z) -> Blender (Z-up, facing -Y).
"""
import json
import math
import os
import sys

import bpy
from mathutils import Matrix, Quaternion, Vector

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from antanim import Clip, load_rig, posed_locals, rest_locals  # noqa: E402
from assetbank import AssetBank  # noqa: E402
from meshset import read_meshset  # noqa: E402

FB_TO_BLENDER = Matrix(((1, 0, 0, 0), (0, 0, -1, 0), (0, 1, 0, 0), (0, 0, 0, 1)))
ROOT_MOTION = {"Reference", "AITrajectory"}
BONE_LENGTH = 0.04


def to_blender(v):
    return (v[0], -v[2], v[1])


def local_matrix(t, q, s):
    return Matrix.LocRotScale(Vector(t), Quaternion((q[3], q[0], q[1], q[2])), Vector(s))


# ---------------------------------------------------------------- skeleton

class Skeleton:
    """Joints of the javelin plus merged gear rigs, with Frostbite-space rest matrices."""

    def __init__(self):
        self.names, self.parents, self.rest_local = [], [], []
        self.rigs = []  # (rig, bone name per joint, first index)

    def add_rig(self, rig, prefix="", attach=None, offset=None):
        """offset: the socket's Frostbite LinearTransform rows (right, up, forward, translation)
        relative to the attach joint."""
        first = len(self.names)
        names = [prefix + j for j, _ in rig["joints"]]
        locals_ = [local_matrix(*l) for l in rest_locals(rig)]
        if attach is not None:
            # Gear sockets (g*_sockets.ebx) name RootSocket = AITrajectory, identity offset:
            # the game puts AITrajectory, not the rig root, on the body socket
            model, joints = [], rig["joints"]
            for (j, parent), l in zip(joints, locals_):
                model.append(model[parent] @ l if parent >= 0 else l)
            traj = next((i for i, (j, _) in enumerate(joints) if j == "AITrajectory"), None)
            if traj is not None:
                locals_[0] = model[traj].inverted() @ locals_[0]
            if offset:
                # Row-vector rows -> column-vector matrix columns
                m = Matrix([offset[0:4], offset[4:8], offset[8:12], offset[12:16]]).transposed()
                m[3] = (0, 0, 0, 1)
                locals_[0] = m @ locals_[0]
        for (j, parent), l in zip(rig["joints"], locals_):
            self.names.append(prefix + j)
            self.parents.append(first + parent if parent >= 0 else attach)
            self.rest_local.append(l)
        self.rigs.append((rig, names, first))
        return names

    def world(self, local):
        out = []
        for i, l in enumerate(local):
            p = self.parents[i]
            out.append(out[p] @ l if p is not None else l)
        return out


def build_armature(name, skel, collection):
    arm = bpy.data.armatures.new(name)
    ob = bpy.data.objects.new(name, arm)
    collection.objects.link(ob)
    bpy.context.view_layer.objects.active = ob
    bpy.ops.object.mode_set(mode="EDIT")
    for bone, w, parent in zip(skel.names, skel.world(skel.rest_local), skel.parents):
        eb = arm.edit_bones.new(bone)
        eb.head, eb.tail = (0, 0, 0), (0, BONE_LENGTH, 0)
        eb.matrix = (FB_TO_BLENDER @ w).normalized()
        if parent is not None:
            eb.parent = arm.edit_bones[skel.names[parent]]
    bpy.ops.object.mode_set(mode="OBJECT")
    arm.display_type = "STICK"
    return ob


# ---------------------------------------------------------------- animation

def key_action(arm_ob, action_name, frames, poses):
    """poses(frame) -> {bone: basis Matrix}; keys location/rotation/scale per bone."""
    act = bpy.data.actions.new(action_name)
    act.use_fake_user = True
    arm_ob.animation_data_create()
    arm_ob.animation_data.action = act
    samples = [poses(f) for f in range(frames)]
    for bone in samples[0]:
        pb = arm_ob.pose.bones[bone]
        pb.rotation_mode = "QUATERNION"
        group = bone
        comps = [m.decompose() for m in (s[bone] for s in samples)]
        for path, n, get in (("location", 3, lambda c: c[0]),
                             ("rotation_quaternion", 4, lambda c: c[1]),
                             ("scale", 3, lambda c: c[2])):
            for i in range(n):
                fc = act.fcurve_ensure_for_datablock(
                    arm_ob, 'pose.bones["%s"].%s' % (bone, path), index=i, group_name=group)
                fc.keyframe_points.add(frames)
                co = []
                for f, c in enumerate(comps):
                    co += [f, get(c)[i]]
                fc.keyframe_points.foreach_set("co", co)
                fc.update()
    return act


def clip_basis(skel, rig_index, clip, frame, skip):
    """Basis matrices for one rig's bones at a clip frame."""
    rig, names, first = skel.rigs[rig_index]
    pose = {k: v for k, v in clip.pose(frame).items() if k.split(".")[0] not in skip}
    out = {}
    for j, l in enumerate(posed_locals(rig, pose)):
        # The rig root is the attachment (root motion is skipped): it stays at rest
        out[names[j]] = Matrix.Identity(4) if j == 0 else \
            skel.rest_local[first + j].inverted() @ local_matrix(*l)
    return out


# ---------------------------------------------------------------- meshes

def material(name, maps):
    mat = bpy.data.materials.get(name)
    if mat:
        return mat
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = nt.nodes["Principled BSDF"]
    if maps.get("emission"):
        bsdf.inputs["Base Color"].default_value = (0.02, 0.01, 0.005, 1)
        bsdf.inputs["Emission Color"].default_value = (1.0, 0.2, 0.0, 1)
        bsdf.inputs["Emission Strength"].default_value = 2.5
        return mat

    def tex(key, colour, x, y):
        node = nt.nodes.new("ShaderNodeTexImage")
        node.image = bpy.data.images.load(maps[key], check_existing=True)
        node.image.colorspace_settings.name = "sRGB" if colour else "Non-Color"
        node.location = (x, y)
        return node

    nt.links.new(tex("basecolor", True, -500, 300).outputs["Color"], bsdf.inputs["Base Color"])
    nt.links.new(tex("roughness", False, -500, 0).outputs["Color"], bsdf.inputs["Roughness"])
    nt.links.new(tex("metallic", False, -500, -250).outputs["Color"], bsdf.inputs["Metallic"])
    nmap = nt.nodes.new("ShaderNodeNormalMap")
    nmap.location = (-200, -500)
    nt.links.new(tex("normal", False, -500, -500).outputs["Color"], nmap.inputs["Color"])
    nt.links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])
    return mat


def skinned_mesh(label, meshset, materials, bone_names, arm_ob, collection, bind=None):
    """bind: Frostbite-space matrix placing the mesh's model space (gear meshes are modelled
    around their own origin and bound at their socket)."""
    for sec_name, verts, faces, uvs, normals, skin in read_meshset(meshset, skin=True)[0]:
        if bind is not None:
            rot = bind.to_3x3()
            verts = [tuple(bind @ Vector(v)) for v in verts]
            normals = [tuple((rot @ Vector(n)).normalized()) for n in normals] if normals else normals
        me = bpy.data.meshes.new("%s_%s" % (label, sec_name))
        me.from_pydata([to_blender(v) for v in verts], [], faces)
        if uvs:
            # A few half-float UVs decode to NaN/inf (Interceptor arms); the FBX exporter rejects them
            uvs = [tuple(c if math.isfinite(c) else 0.0 for c in t) for t in uvs]
            uv = me.uv_layers.new(name="UVMap")
            uv.data.foreach_set("uv", [c for p in me.polygons for li in p.loop_indices
                                       for c in uvs[me.loops[li].vertex_index]])
        me.update()
        if normals:
            n = [to_blender(x) for x in normals]
            agree = sum((p.normal.x * n[p.vertices[0]][0] + p.normal.y * n[p.vertices[0]][1]
                         + p.normal.z * n[p.vertices[0]][2]) > 0 for p in me.polygons)
            if agree < len(me.polygons) / 2:
                me.flip_normals()
            me.normals_split_custom_set_from_vertices(n)
        maps = materials.get(sec_name) or next(iter(materials.values()))
        me.materials.append(material("%s_%s" % (label, sec_name) if maps.get("emission")
                                     else os.path.basename(maps["basecolor"])[:-14], maps))
        ob = bpy.data.objects.new(me.name, me)
        collection.objects.link(ob)
        # Group (bone, weight) -> vertices, so each vertex group takes few calls
        groups = {}
        for v, inf in enumerate(skin):
            for joint, w in inf:
                groups.setdefault((joint, w), []).append(v)
        for (joint, w), vs in groups.items():
            bone = bone_names[joint]
            vg = ob.vertex_groups.get(bone) or ob.vertex_groups.new(name=bone)
            vg.add(vs, w, "ADD")
        ob.parent = arm_ob
        ob.modifiers.new("Armature", "ARMATURE").object = arm_ob
        print("mesh", ob.name, len(verts), "verts")


# ---------------------------------------------------------------- preview

def studio_render(scene, out_dir, name):
    world = scene.world or bpy.data.worlds.new("World")
    scene.world = world
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.32, 0.36, 0.42, 1)
    world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.6

    def light(n, energy, loc, rot, size, colour=(1, 1, 1)):
        ld = bpy.data.lights.new(n, "AREA")
        ld.energy, ld.size, ld.color = energy, size, colour
        ob = bpy.data.objects.new(n, ld)
        ob.location, ob.rotation_euler = loc, [math.radians(a) for a in rot]
        scene.collection.objects.link(ob)
        return ob

    rig_lights = [light("Key", 900, (2.5, -3.5, 4.0), (50, 0, 35), 3.0, (1, 0.96, 0.9)),
                  light("Fill", 350, (-3.5, -2.5, 2.5), (65, 0, -55), 4.0, (0.85, 0.9, 1)),
                  light("Rim", 700, (0, 3.5, 3.5), (-50, 0, 180), 3.0)]
    cam = bpy.data.objects.new("PreviewCamera", bpy.data.cameras.new("PreviewCamera"))
    scene.collection.objects.link(cam)
    scene.camera = cam
    engines = [e.identifier for e in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items]
    scene.render.engine = "BLENDER_EEVEE" if "BLENDER_EEVEE" in engines else "BLENDER_EEVEE_NEXT"
    scene.render.resolution_x, scene.render.resolution_y = 900, 1100
    scene.view_settings.view_transform = "AgX"
    for view, loc, yaw in (("front", (0, -6.6, 1.25), 0), ("back", (0, 6.6, 1.25), 180)):
        cam.location, cam.rotation_euler = loc, (math.radians(90), 0, math.radians(yaw))
        scene.render.filepath = os.path.join(out_dir, "%s_%s.png" % (name, view))
        bpy.ops.render.render(write_still=True)
    # Keep the exported FBX to the character only
    for ob in rig_lights + [cam]:
        bpy.data.objects.remove(ob)


# ---------------------------------------------------------------- main

def main():
    job = json.load(open(sys.argv[sys.argv.index("--") + 1]))
    name, out_dir = job["name"], job["out_dir"]
    os.makedirs(out_dir, exist_ok=True)
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob)
    scene = bpy.context.scene
    col = bpy.data.collections.new(name)
    scene.collection.children.link(col)

    # Clips (gear rig i + 1 in the skeleton is job["gear"][i])
    gear_clips = []  # (rig index, clip, paired body clip or None)
    stowed = {}
    body_rig = load_rig(job["skeleton"])
    body_prefix = job["skeleton"].split("_")[0]
    for gi, g in enumerate(job["gear"]):
        if not g.get("bank"):
            continue
        rig = load_rig(g["rig"])
        gear_prefix = g["rig"].split("_")[0]
        bank = AssetBank(g["bank"])
        have = {o.get("__name") for o in bank.objects if o["__type"] == "ClipControllerAsset"}
        for n in g["clips"]:
            if n not in have:
                continue
            try:
                clip = Clip(bank, n, rig)
            except Exception as e:
                print("skipped clip", n, e)
                continue
            if clip.additive:
                print("skipped additive clip", n)
                continue
            # The game plays the javelin's clip of the same name with the gear's
            # (GCMS_MobileShield_Aim_Idle_FM with EXH_MobileShield_Aim_Idle_FM)
            body = None
            paired = body_prefix + n[len(gear_prefix):]
            if paired in have:
                try:
                    body = Clip(bank, paired, body_rig)
                    body = None if body.additive else body
                except Exception as e:
                    print("skipped body clip", paired, e)
            gear_clips.append((gi + 1, clip, body))
            if n == g.get("stowed"):
                stowed[gi + 1] = clip

    # Gear that stows collapsed also gets an extended copy (own bones, held in the game's
    # deployed idle) in its own collection, so either one can be hidden
    extended = {}  # gear index -> deployed idle clip
    for gi, g in enumerate(job["gear"]):
        if gi + 1 in stowed and "Collapsed" in stowed[gi + 1].name:
            idles = [c for ri, c, _ in gear_clips if ri == gi + 1 and "InAir" not in c.name]
            deployed = [c for c in idles if "Aim_Idle" in c.name] \
                + [c for c in idles if "Draw_Idle" in c.name]
            if deployed:
                extended[gi] = deployed[0]

    skel = Skeleton()
    body_bones = skel.add_rig(load_rig(job["skeleton"]))
    prefixes = []
    for g in job["gear"]:
        prefixes.append(g["rig"].split("_")[0] + "_")
        skel.add_rig(load_rig(g["rig"]), prefixes[-1], attach=body_bones.index(g["socket"]),
                     offset=g.get("socket_offset"))
    ext_rigs = {}  # rig index -> deployed clip
    for gi, clip in extended.items():
        g = job["gear"][gi]
        ext_rigs[len(skel.rigs)] = clip
        skel.add_rig(load_rig(g["rig"]), prefixes[gi] + "Ext_",
                     attach=body_bones.index(g["socket"]), offset=g.get("socket_offset"))
    arm_ob = build_armature(name + "_rig", skel, col)

    # Mesh blend indices follow the render skeleton's bone order, not the rig's joints
    for m in job["meshes"]:
        skinned_mesh(m["label"], m["meshset"], m["materials"], job["render_bones"], arm_ob, col)
    rest_world = skel.world(skel.rest_local)
    gear_meshes = [(gi, prefixes[gi], skel.rigs[gi + 1][2], "") for gi in range(len(job["gear"]))]
    gear_meshes += [(gi, prefixes[gi] + "Ext_", skel.rigs[ri][2], "extended")
                    for ri, gi in zip(ext_rigs, extended)]
    for gi, prefix, first, state in gear_meshes:
        g = job["gear"][gi]
        target = col
        if gi in extended:
            target = bpy.data.collections.new("%s %s" % (g["label"], state or "collapsed"))
            col.children.link(target)
        skinned_mesh(g["label"] + ("_" + state if state else ""), g["meshset"], g["materials"],
                     [prefix + b for b in g["render_bones"]], arm_ob, target,
                     bind=rest_world[first])

    def hold_extended(out, f=0):
        for ri, clip in ext_rigs.items():
            out.update(clip_basis(skel, ri, clip, min(f, clip.frames - 1), ROOT_MOTION))
        return out

    body_clip = None
    if job.get("pose"):
        body_clip = Clip(AssetBank(job["pose"]["bank"]), job["pose"]["clip"],
                         load_rig(job["skeleton"]))
    frames = body_clip.frames if body_clip else 1

    def forge_pose(f):
        out = {}
        if body_clip:
            out.update(clip_basis(skel, 0, body_clip, f, ROOT_MOTION))
        for ri, clip in stowed.items():
            out.update(clip_basis(skel, ri, clip, min(f, clip.frames - 1), ROOT_MOTION))
        return hold_extended(out, f)

    # The rig's rest pose is the game's bind A-pose; gear rests unfolded, so key it stowed
    def a_pose(f):
        out = {b: Matrix.Identity(4) for b in skel.rigs[0][1]}
        for ri, clip in stowed.items():
            out.update(clip_basis(skel, ri, clip, 0, ROOT_MOTION))
        return hold_extended(out)

    key_action(arm_ob, name + "_APose", 1, a_pose)
    main_action = key_action(arm_ob, name + "_Forge", frames, forge_pose)
    ext_of = {gi + 1: xi for xi, gi in zip(ext_rigs, extended)}  # gear rig -> its extended copy

    def gear_pose(f, ri, clip, body):
        out = {}
        if body:
            out.update(clip_basis(skel, 0, body, min(f, body.frames - 1), ROOT_MOTION))
        for r in (ri, ext_of.get(ri)):  # the extended copy plays it too
            if r is not None:
                out.update(clip_basis(skel, r, clip, min(f, clip.frames - 1), ROOT_MOTION))
        return out

    for ri, clip, body in gear_clips:
        key_action(arm_ob, clip.name, max(clip.frames, body.frames if body else 0),
                   lambda f, ri=ri, clip=clip, body=body: gear_pose(f, ri, clip, body))
        print("clip", clip.name, clip.frames, "frames", ("+ body " + body.name) if body else "")
    arm_ob.animation_data.action = main_action
    scene.frame_start, scene.frame_end = 0, frames - 1
    scene.render.fps = 30
    scene.frame_set(0)

    if job.get("render", True):
        # Previews show the collapsed copies only
        ext_cols = [c for c in col.children if c.name.endswith(" extended")]
        for c in ext_cols:
            c.hide_render = True
        studio_render(scene, out_dir, name)
        for c in ext_cols:
            c.hide_render = False
    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(out_dir, name + ".blend"))
    bpy.ops.export_scene.fbx(
        filepath=os.path.join(out_dir, name + ".fbx"), object_types={"ARMATURE", "MESH"},
        add_leaf_bones=False, bake_anim=True, bake_anim_use_all_actions=True,
        bake_anim_use_nla_strips=False, bake_anim_force_startend_keying=True,
        path_mode="COPY", embed_textures=True, use_custom_props=False)
    print("exported", os.path.join(out_dir, name + ".fbx"))


if __name__ == "__main__":
    main()
