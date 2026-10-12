"""
Builder settings in config.json next to this file, detected on first use:
  game        the Anthem install folder (holds Data/, Patch/, oo2core_7_win64.dll)
  bforartists Bforartists or Blender executable used for the build
  output      where builds go
Game files are extracted from the install on demand into data/game/ (see gamefs.py).
Pure Python (also imported inside Bforartists).
"""
import glob
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "config.json")
DATA = os.path.join(HERE, "data")  # game index + extracted files, rigs.json, catalogs, caches
GAME_FILES = os.path.join(DATA, "game")


def valid_game(path):
    return bool(path) and os.path.exists(os.path.join(path, "Data", "layout.toc")) \
        and os.path.exists(os.path.join(path, "oo2core_7_win64.dll"))


def _detect_game():
    candidates = []
    try:
        import winreg
        for key in (r"SOFTWARE\WOW6432Node\BioWare\Anthem", r"SOFTWARE\BioWare\Anthem"):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
                    candidates.append(winreg.QueryValueEx(k, "Install Dir")[0])
            except OSError:
                pass
    except ImportError:
        pass
    for drive in "CDEFGH":
        for pat in (r"%s:\Program Files (x86)\Origin Games\Anthem",
                    r"%s:\Program Files\EA Games\Anthem", r"%s:\Program Files\Origin Games\Anthem",
                    r"%s:\Games\Anthem", r"%s:\Anthem"):
            candidates.append(pat % drive)
    candidates += [os.path.dirname(p) for p in
                   glob.glob(r"C:\Users\*\Downloads\*\Anthem.exe")
                   + glob.glob(r"C:\Users\*\Downloads\*\*\Anthem.exe")]
    return next((c for c in candidates if valid_game(c)), None)


def _detect_blender():
    patterns = [r"C:\Program Files\Bforartists\*\bforartists.exe",
                r"C:\Program Files\Blender Foundation\Blender*\blender.exe"]
    for pat in patterns:
        found = sorted(glob.glob(pat))
        if found:
            return found[-1]
    return None


def load():
    cfg = json.load(open(PATH)) if os.path.exists(PATH) else {}
    cfg.pop("export", None)
    if not valid_game(cfg.get("game")):
        cfg["game"] = _detect_game()
    if not (cfg.get("bforartists") and os.path.exists(cfg["bforartists"])):
        cfg["bforartists"] = _detect_blender()
    cfg.setdefault("output", os.path.join(os.path.dirname(HERE), "builds"))
    return cfg


def save(cfg):
    json.dump(cfg, open(PATH, "w"), indent=1)


def export_root():
    """Top of the extracted game files (bundle/ plus the TOC chunks/)."""
    return GAME_FILES


def bundle():
    return os.path.join(GAME_FILES, "bundle")
