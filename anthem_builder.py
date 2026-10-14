"""
Anthem armour builder: pick a javelin, an armour set and gear; get an FBX for Blender.

  python anthem_builder.py                      window
  python anthem_builder.py --list [JAVELIN]     armour sets and gear
  python anthem_builder.py --javelin Colossus --set darkgrace
                           [--gear wrist_siegeshield --gear shoulder_mortar ...]
                           [--part torso=outlaw] [--palette "Dark Grace"] [--out DIR]

Setup is automatic: missing Python packages are installed, Bforartists (or Blender) and
the Anthem install are found (or asked for once, saved in config.json). The install is
indexed once; each javelin's files are then extracted from it and catalogued the first
time the javelin is selected, and mesh/texture data as builds need it (data/).
Output: <out>/<Javelin>_<set>/<Javelin>_<set>.fbx (+ .blend, front/back previews, maps), and
<Javelin>_<set>_APose.fbx, the meshes frozen in the A-pose with no armature.
"""
import argparse
import importlib
import importlib.util
import json
import os
import struct
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402

PARTS = ("helm", "torso", "arms", "legs")
PACKAGES = {"numpy": "numpy", "PIL": "pillow", "texture2ddecoder": "texture2ddecoder"}


def log_print(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- setup

def ensure_packages(log=log_print):
    missing = [pip for mod, pip in PACKAGES.items() if importlib.util.find_spec(mod) is None]
    if missing:
        log("Installing Python packages: %s ..." % ", ".join(missing))
        subprocess.run([sys.executable, "-m", "pip", "install", "--user", "--quiet"] + missing,
                       check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        importlib.invalidate_caches()


def setup_problems(cfg):
    problems = []
    if not config.valid_game(cfg.get("game")):
        problems.append("game")
    if not (cfg.get("bforartists") and os.path.exists(cfg["bforartists"])):
        problems.append("bforartists")
    return problems


# ---------------------------------------------------------------- job

def resolve_parts(cat, armour_set, overrides):
    """Part -> mesh entries: the set's parts, per-part overrides, gaps from 'default'."""
    sets = cat["sets"]
    parts = {}
    for part in PARTS:
        src = overrides.get(part, armour_set)
        if part in sets.get(src, {}):
            parts[part] = sets[src][part]
        elif part in sets.get("default", {}):
            parts[part] = sets["default"][part]
    return parts


def section_names(meshset):
    b = open(meshset, "rb").read()
    lod = struct.unpack_from("<Q", b, 0x20)[0]
    count, sec_off = struct.unpack_from("<IQ", b, lod + 0x0C)
    names = []
    for i in range(count):
        off = struct.unpack_from("<Q", b, sec_off + i * 0xE0 + 0x08)[0]
        name = b[off:b.index(b"\0", off)].decode("ascii", "replace")
        if name:
            names.append(name)
    return names


def pick_texset(section, texsets):
    """Texture set for a mesh section, by the part named in the section."""
    s = section.lower()
    base = [k for k in texsets if "_base" in k and "bitpack" not in k]
    other = [k for k in texsets if k not in base]
    if s.startswith("base"):
        return (base or other or [None])[0]
    for word in ("helm", "torso", "arms", "legs", "cape", "tabard", "sash"):
        if word in s:
            hit = [k for k in other if word in k]
            if hit:
                return hit[0]
    return (other or base or [None])[0]


def section_materials(entry, palette, maps_dir, log):
    from materials import bake
    out = {}
    for sec in section_names(entry["meshset"]):
        if any(w in sec.lower() for w in ("visor", "glass", "emissive", "light")):
            out[sec] = {"emission": True}
            continue
        key = pick_texset(sec, entry["textures"])
        if key is None:
            continue
        frame = "_base" in key and "bitpack" not in key
        log("  maps %s -> %s" % (sec, key))
        out[sec] = bake(key, entry["textures"][key], palette, frame, maps_dir,
                        os.path.join(config.DATA, "textures"))
    return out


def slot_of(g):
    return g["socket"], tuple(g["socket_offset"])


def make_job(cat, armour_set, overrides, gear, palette, out_root, render, log):
    name = "%s_%s" % (cat["title"], armour_set)
    out_dir = os.path.join(out_root, name)
    maps_dir = os.path.join(out_dir, "maps_" + palette.replace(" ", ""))
    log("Baking maps (%s palette) ..." % palette)
    meshes = [{"label": "Frame", "meshset": cat["frame"]["meshset"],
               "materials": section_materials(cat["frame"], palette, maps_dir, log)}]
    for part, entries in resolve_parts(cat, armour_set, overrides).items():
        for e in entries:
            meshes.append({"label": part.title(), "meshset": e["meshset"],
                           "materials": section_materials(e, palette, maps_dir, log)})
    gear_jobs = []
    taken = {}
    for label in gear:
        # One item per socket, as in game; pieces on one socket can also share a rig
        slot = slot_of(cat["gear"][label])
        if slot in taken:
            raise ValueError("%s and %s both mount on %s; pick one"
                             % (taken[slot], label, cat["gear"][label]["socket"]))
        taken[slot] = label
        g = cat["gear"][label]
        gear_jobs.append({"label": label, "meshset": g["mesh"]["meshset"],
                          "materials": section_materials(g["mesh"], palette, maps_dir, log),
                          "rig": g["rig"], "socket": g["socket"],
                          "socket_offset": g["socket_offset"], "bank": g["bank"],
                          "render_bones": g["render_bones"], "clips": g["clips"],
                          "stowed": g["stowed"]})
    return {"name": name, "out_dir": out_dir, "skeleton": cat["skeleton"],
            "render_bones": cat["render_bones"], "pose": cat["pose"], "meshes": meshes,
            "gear": gear_jobs, "render": render}


def run_build(job, log):
    os.makedirs(job["out_dir"], exist_ok=True)
    job_path = os.path.join(job["out_dir"], "job.json")
    json.dump(job, open(job_path, "w"), indent=1)
    log("Building in %s ..." % os.path.basename(config.load()["bforartists"]))
    cmd = [config.load()["bforartists"], "--background", "--factory-startup", "--python",
           os.path.join(HERE, "build_fbx.py"), "--", job_path]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            errors="replace",
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    tail = []
    for line in proc.stdout:
        line = line.rstrip()
        tail = (tail + [line])[-15:]
        if line.startswith(("mesh ", "clip ", "skipped", "exported")) or "Error" in line \
                or "Traceback" in line or "Saved:" in line:
            log("  " + line)
    if proc.wait() != 0:
        log("\n".join(tail))
        raise RuntimeError("Bforartists build failed (exit %d)" % proc.returncode)
    fbx = os.path.join(job["out_dir"], job["name"] + ".fbx")
    if not os.path.exists(fbx):
        raise RuntimeError("No FBX was written")
    log("Done: " + fbx)
    return fbx


# ---------------------------------------------------------------- CLI

def find_javelin(name):
    import catalog
    for k, t in catalog.javelins().items():
        if name.lower() in (k.lower(), t.lower()):
            return k
    raise SystemExit("Unknown javelin %s; choose from %s"
                     % (name, ", ".join(catalog.javelins().values())))


def cli(args):
    ensure_packages()
    cfg = config.load()
    if args.game:
        cfg["game"] = args.game
    if args.bforartists:
        cfg["bforartists"] = args.bforartists
    problems = setup_problems(cfg)
    if problems:
        raise SystemExit("Not found: %s. Pass --game <Anthem folder> / --bforartists <exe>."
                         % ", ".join(problems))
    config.save(cfg)
    import gamefs
    gamefs.db()
    import catalog
    if args.list is not None:
        keys = [find_javelin(args.list)] if args.list else list(catalog.javelins())
        for k in keys:
            cat = catalog.load(k)
            print("%s: skeleton %s, pose %s" % (cat["title"], cat["skeleton"],
                                                cat["pose"]["clip"] if cat["pose"] else "bind"))
            for s in sorted(cat["sets"]):
                print("    %-22s %s" % (s, ", ".join(p for p in PARTS if p in cat["sets"][s])))
            for g, gi in sorted(cat["gear"].items()):
                print("    gear %-26s -> %s" % (g, gi["socket"]))
        return
    cat = catalog.load(find_javelin(args.javelin))
    if args.set not in cat["sets"]:
        raise SystemExit("Unknown set %s; see --list %s" % (args.set, cat["title"]))
    overrides = dict(p.split("=", 1) for p in args.part)
    job = make_job(cat, args.set, overrides, args.gear, args.palette,
                   args.out or cfg["output"], not args.no_render, log_print)
    run_build(job, log_print)


# ---------------------------------------------------------------- window

def gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    root = tk.Tk()
    root.title("Anthem Armour Builder")
    root.geometry("780x660")
    status = tk.StringVar(value="Starting ...")
    logbox = tk.Text(root, height=12, state="disabled", wrap="none")

    def log(msg):
        def add():
            logbox.configure(state="normal")
            logbox.insert("end", msg + "\n")
            logbox.see("end")
            logbox.configure(state="disabled")
        root.after(0, add)

    top = ttk.Frame(root, padding=8)
    top.pack(fill="both", expand=True)
    ttk.Label(top, text="Javelin").grid(row=0, column=0, sticky="w")
    jav_var = tk.StringVar()
    jav_box = ttk.Combobox(top, textvariable=jav_var, state="readonly", width=20)
    jav_box.grid(row=0, column=1, sticky="w")
    ttk.Label(top, text="Palette").grid(row=0, column=2, sticky="e", padx=(16, 4))
    pal_var = tk.StringVar(value="Dark Grace")
    pal_box = ttk.Combobox(top, textvariable=pal_var, state="readonly", width=14)
    pal_box.grid(row=0, column=3, sticky="w")

    ttk.Label(top, text="Armour set").grid(row=1, column=0, sticky="nw", pady=(8, 0))
    sets_list = tk.Listbox(top, height=14, exportselection=False)
    sets_list.grid(row=1, column=1, sticky="nsew", pady=(8, 0))
    parts_frame = ttk.LabelFrame(top, text="Per-part override", padding=6)
    parts_frame.grid(row=1, column=2, columnspan=2, sticky="new", padx=(16, 0), pady=(8, 0))
    part_vars, part_boxes = {}, {}
    for i, part in enumerate(PARTS):
        ttk.Label(parts_frame, text=part.title()).grid(row=i, column=0, sticky="w")
        part_vars[part] = tk.StringVar(value="(set)")
        part_boxes[part] = ttk.Combobox(parts_frame, textvariable=part_vars[part],
                                        state="readonly", width=18)
        part_boxes[part].grid(row=i, column=1, sticky="w", pady=1)
    gear_frame = ttk.LabelFrame(top, text="Gear (mounted on its socket)", padding=6)
    gear_frame.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(8, 0))
    gear_vars = {}

    out_var = tk.StringVar()
    ttk.Label(top, text="Output").grid(row=3, column=0, sticky="w", pady=(8, 0))
    ttk.Entry(top, textvariable=out_var, width=60).grid(row=3, column=1, columnspan=2,
                                                        sticky="ew", pady=(8, 0))
    ttk.Button(top, text="...", width=3,
               command=lambda: out_var.set(filedialog.askdirectory() or out_var.get())
               ).grid(row=3, column=3, sticky="w", pady=(8, 0))
    build_btn = ttk.Button(top, text="Build FBX", state="disabled")
    build_btn.grid(row=4, column=1, sticky="w", pady=8)
    ttk.Label(top, textvariable=status).grid(row=4, column=2, columnspan=2, sticky="w")
    top.columnconfigure(1, weight=1)
    top.rowconfigure(1, weight=1)
    logbox.pack(fill="both", expand=False, padx=8, pady=(0, 8))

    state = {"cats": {}, "titles": {}}

    def busy(on, text=""):
        build_btn.state(["disabled"] if on else ["!disabled"])
        jav_box.state(["disabled"] if on else ["!disabled", "readonly"])
        if text:
            status.set(text)

    def show(cat):
        sets_list.delete(0, "end")
        for s in sorted(cat["sets"]):
            sets_list.insert("end", s)
        for part in PARTS:
            part_boxes[part]["values"] = ["(set)"] + sorted(s for s in cat["sets"]
                                                            if part in cat["sets"][s])
            part_vars[part].set("(set)")
        for w in gear_frame.winfo_children():
            w.destroy()
        gear_vars.clear()
        def one_per_socket(picked):
            # Ticking a piece unticks the others on its socket
            if gear_vars[picked].get():
                for other, var in gear_vars.items():
                    if other != picked and slot_of(cat["gear"][other]) == slot_of(cat["gear"][picked]):
                        var.set(False)

        for i, (g, gi) in enumerate(sorted(cat["gear"].items())):
            gear_vars[g] = tk.BooleanVar(value=False)
            ttk.Checkbutton(gear_frame, text="%s  (%s)" % (g, gi["socket"].replace("Socket", "")),
                            variable=gear_vars[g], command=lambda g=g: one_per_socket(g)).grid(
                row=i // 3, column=i % 3, sticky="w", padx=(0, 12))
        busy(False, "%s: %d armour sets, pose %s" % (
            cat["title"], len(cat["sets"]), cat["pose"]["clip"] if cat["pose"] else "bind"))

    def on_javelin(*_):
        key = state["titles"][jav_var.get()]
        if key in state["cats"]:
            show(state["cats"][key])
            return
        busy(True, "Cataloguing %s (first time only) ..." % jav_var.get())

        def work():
            import catalog
            try:
                cat = catalog.load(key, log)
                state["cats"][key] = cat
                root.after(0, lambda: show(cat))
            except Exception as e:
                log("FAILED: %s" % e)
                root.after(0, lambda: busy(False, "Catalog failed, see log"))
        threading.Thread(target=work, daemon=True).start()

    def on_build():
        sel = sets_list.curselection()
        if not sel:
            messagebox.showinfo("Anthem Armour Builder", "Pick an armour set first.")
            return
        cat = state["cats"][state["titles"][jav_var.get()]]
        armour_set = sets_list.get(sel[0])
        overrides = {p: v.get() for p, v in part_vars.items() if v.get() != "(set)"}
        gear = [g for g, v in gear_vars.items() if v.get()]
        cfg = config.load()
        cfg["output"] = out_var.get()
        config.save(cfg)
        busy(True, "Building %s %s ..." % (cat["title"], armour_set))

        def work():
            try:
                job = make_job(cat, armour_set, overrides, gear, pal_var.get(),
                               out_var.get(), True, log)
                fbx = run_build(job, log)
                root.after(0, lambda: busy(False, "Done: " + os.path.basename(fbx)))
                os.startfile(os.path.dirname(fbx))
            except Exception as e:
                log("FAILED: %s" % e)
                root.after(0, lambda: busy(False, "Failed, see log"))
        threading.Thread(target=work, daemon=True).start()

    build_btn.configure(command=on_build)
    jav_box.bind("<<ComboboxSelected>>", on_javelin)

    def ask_setup(cfg):
        """Ask for anything that could not be found; returns False if cancelled."""
        while not config.valid_game(cfg.get("game")):
            messagebox.showinfo("Anthem Armour Builder",
                                "Select the Anthem install folder (it contains Anthem.exe, "
                                "Data and Patch).")
            p = filedialog.askdirectory(title="Anthem install folder")
            if not p:
                return False
            cfg["game"] = os.path.normpath(p)
        while not (cfg.get("bforartists") and os.path.exists(cfg["bforartists"])):
            messagebox.showinfo("Anthem Armour Builder", "Select bforartists.exe or blender.exe.")
            p = filedialog.askopenfilename(title="Bforartists or Blender",
                                           filetypes=[("Program", "*.exe")])
            if not p:
                return False
            cfg["bforartists"] = os.path.normpath(p)
        config.save(cfg)
        return True

    def start():
        def packages():
            try:
                ensure_packages(log)
                root.after(0, ready)
            except Exception as e:
                log("FAILED to install Python packages: %s" % e)
                root.after(0, lambda: status.set("Package install failed, see log"))

        def ready():
            cfg = config.load()
            if setup_problems(cfg) and not ask_setup(cfg):
                status.set("Setup cancelled")
                return
            status.set("Indexing the game install ...")

            def index():
                try:
                    import gamefs
                    gamefs.db(log)
                    root.after(0, populate)
                except Exception as e:
                    log("FAILED to index the game: %s" % e)
                    root.after(0, lambda: status.set("Game indexing failed, see log"))
            threading.Thread(target=index, daemon=True).start()

        def populate():
            from materials import PALETTES
            import catalog
            pal_box["values"] = list(PALETTES)
            out_var.set(config.load()["output"])
            state["titles"] = {t: k for k, t in catalog.javelins().items()}
            jav_box["values"] = list(state["titles"])
            jav_var.set(next(iter(state["titles"])))
            on_javelin()
        threading.Thread(target=packages, daemon=True).start()

    root.after(100, start)
    root.mainloop()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--list", nargs="?", const="", metavar="JAVELIN")
    ap.add_argument("--javelin")
    ap.add_argument("--set")
    ap.add_argument("--part", action="append", default=[], help="part=set, e.g. torso=outlaw")
    ap.add_argument("--gear", action="append", default=[])
    ap.add_argument("--palette", default="Dark Grace")
    ap.add_argument("--out")
    ap.add_argument("--game", help="Anthem install folder")
    ap.add_argument("--bforartists", help="bforartists.exe or blender.exe")
    ap.add_argument("--no-render", action="store_true")
    args = ap.parse_args()
    if args.list is not None or args.javelin:
        if args.list is None and not args.set:
            ap.error("--set is required with --javelin")
        cli(args)
    else:
        gui()


if __name__ == "__main__":
    main()
