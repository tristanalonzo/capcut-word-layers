"""
Editing engine for CapCut Word Layers.

Reads a CapCut desktop project (draft_content.json), edits its text clips and
writes it back. No UI code lives here, so everything can be tested on its own.

Times are microseconds. Positions are CapCut "transform" units: x is in half
canvas widths, y in half canvas heights, y pointing up.
"""
import copy
import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path

DRAFT_NAME = "draft_content.json"
BACKUP_DIR = "_wordlayers_backups"
STATE_NAME = "_wordlayers_state.json"
SETTINGS_FILE = Path.home() / ".capcut_word_layers.json"
UNDO_LIMIT = 50

DEFAULT_SETTINGS = {
    "short_max": 4,        # a word with this many letters or fewer is "short"
    "group_max": 4,        # most words stacked in one group
    "auto_offset": True,   # place stacked words on separate lines
    "gap": 1.0,            # distance between stacked words, in line heights
    "shift_x": 0.0,        # sideways step per word, in font sizes
    "preview_scale": 5.3,  # preview pixels per CapCut size point at 1080p
}

# Fields of a text material that belong to the words, not to the look.
NOT_STYLE = {"id", "content", "base_content", "words", "current_words", "name",
             "recognize_task_id", "recognize_text", "recognize_model", "punc_model",
             "recognize_type", "text_to_audio_ids", "group_id", "relevance_segment",
             "ssml_content", "translate_original_text", "type", "sub_type"}


class EditError(Exception):
    """A problem the user can understand and act on."""


# ------------------------------------------------------------------ small helpers

def new_id():
    """New unique ID in CapCut's own format (uppercase, third group lowercase)."""
    p = str(uuid.uuid4()).upper().split("-")
    p[2] = p[2].lower()
    return "-".join(p)


def rgb_hex(c):
    try:
        return "#%02X%02X%02X" % tuple(max(0, min(255, round(float(v) * 255))) for v in c[:3])
    except (TypeError, ValueError):
        return "#FFFFFF"


def hex_rgb(h):
    h = str(h).lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", h):
        raise EditError(f"Not a color: #{h}")
    return [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]


def letters(word):
    return sum(ch.isalnum() for ch in word)


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# Style ranges may be counted in different units depending on the CapCut version.
UNITS = [
    lambda s: len(s),                              # characters
    lambda s: len(s.encode("utf-16-le")) // 2,     # UTF-16 units
    lambda s: len(s.encode("utf-16-le")),          # UTF-16 bytes
    lambda s: len(s.encode("utf-8")),              # UTF-8 bytes
]


def valid_range(st):
    r = st.get("range") if isinstance(st, dict) else None
    return isinstance(r, list) and len(r) == 2 and all(isinstance(x, int) for x in r)


def detect_unit(text, styles):
    ends = [s["range"][1] for s in styles if valid_range(s)]
    if ends:
        for measure in UNITS:
            if measure(text) == max(ends):
                return measure
    return UNITS[1]


def read_content(material):
    """Parsed content of a text material, or None if it is not plain text."""
    try:
        content = json.loads(material.get("content", ""))
    except (TypeError, ValueError, AttributeError):
        return None
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content
    return None


def split_content(content):
    """[(word, content_json_string), ...] with style ranges cut down to each word."""
    text = content["text"]
    styles = content.get("styles") or []
    measure = detect_unit(text, styles)
    out = []
    for m in re.finditer(r"\S+", text):
        ws, we = measure(text[:m.start()]), measure(text[:m.end()])
        new_styles = []
        for st in styles:
            if not valid_range(st):
                continue
            a, b = max(st["range"][0], ws), min(st["range"][1], we)
            if a < b:
                ns = copy.deepcopy(st)
                ns["range"] = [a - ws, b - ws]
                new_styles.append(ns)
        if not new_styles and styles:
            ns = copy.deepcopy(styles[0])
            ns["range"] = [0, we - ws]
            new_styles.append(ns)
        nc = copy.deepcopy(content)
        nc["text"] = m.group()
        if "styles" in content:
            nc["styles"] = new_styles
        out.append((m.group(), dumps(nc)))
    return out


def fit_animations(material, clip_dur):
    """Shrink in/out animations so they fit inside a shorter clip."""
    anims = material.get("animations")
    if not isinstance(anims, list):
        return

    def ok(a, kind):
        return (isinstance(a, dict) and a.get("type") == kind
                and isinstance(a.get("duration"), (int, float)))
    ins = [a for a in anims if ok(a, "in")]
    outs = [a for a in anims if ok(a, "out")]
    total = sum(a["duration"] for a in ins + outs)
    scale = min(1.0, clip_dur / total) if total > 0 else 1.0
    for a in ins + outs:
        a["duration"] = int(a["duration"] * scale)
    for a in ins:
        a["start"] = 0
    for a in outs:
        a["start"] = max(0, clip_dur - a["duration"])


def seg_start(seg):
    return seg["target_timerange"]["start"]


def seg_end(seg):
    tr = seg["target_timerange"]
    return tr["start"] + tr["duration"]


def has_time(seg):
    tr = seg.get("target_timerange") if isinstance(seg, dict) else None
    return (isinstance(tr, dict) and isinstance(tr.get("start"), int)
            and isinstance(tr.get("duration"), int))


def chunk_sizes(n, biggest):
    """Split n words into groups no larger than `biggest`, as evenly as possible."""
    k = -(-n // biggest)
    base, extra = divmod(n, k)
    return [base + 1] * extra + [base] * (k - extra)


# ------------------------------------------------------------------ files

def drafts_root():
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    p = Path(local) / "CapCut" / "User Data" / "Projects" / "com.lveditor.draft"
    return p if p.is_dir() else None


def list_projects(limit=40):
    root = drafts_root()
    if not root:
        return []
    found = []
    for p in root.iterdir():
        f = p / DRAFT_NAME
        if f.is_file():
            found.append({"name": p.name, "path": str(p), "mtime": f.stat().st_mtime})
    found.sort(key=lambda d: d["mtime"], reverse=True)
    return found[:limit]


def find_draft_files(project):
    files = []
    main = project / DRAFT_NAME
    if main.is_file():
        files.append(main)
    timelines = project / "Timelines"
    if timelines.is_dir():
        files += sorted(timelines.rglob(DRAFT_NAME))
    return files


def capcut_running():
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq CapCut.exe"],
                             capture_output=True, text=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return "CapCut.exe" in out
    except Exception:
        return False


def _settings_file():
    try:
        d = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_settings_file(d):
    try:
        SETTINGS_FILE.write_text(json.dumps(d, indent=1), encoding="utf-8")
    except OSError:
        pass


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    s.update({k: v for k, v in _settings_file().items() if k in s and type(v) in (int, float, bool)})
    return s


def save_settings(settings):
    d = _settings_file()
    d.update(settings)
    _write_settings_file(d)


def is_glow(obj):
    """CapCut stores a text glow as its own effect material, linked from the clip."""
    return isinstance(obj, dict) and (obj.get("panel_id") == "text_glow" or obj.get("type") == "bloom")


def load_glow_template():
    t = _settings_file().get("glow_template")
    return t if is_glow(t) else None


def save_glow_template(effect):
    d = _settings_file()
    d["glow_template"] = effect
    _write_settings_file(d)


def load_documents(project):
    """
    Group the project's draft files into documents. Files that describe the same
    timeline (same draft id, same clips) are one document and are saved together.
    """
    docs = {}
    for f in find_draft_files(project):
        raw = f.read_bytes()
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError):
            continue
        if not isinstance(data, dict) or not isinstance(data.get("tracks"), list):
            continue
        seg_ids = frozenset(s.get("id") for t in data["tracks"] if isinstance(t, dict)
                            for s in t.get("segments", []) if isinstance(s, dict))
        doc = docs.setdefault((data.get("id"), seg_ids), {"files": [], "hashes": {}, "data": data})
        doc["files"].append(f)
        doc["hashes"][str(f)] = hashlib.sha256(raw).hexdigest()
    out = list(docs.values())
    for i, d in enumerate(out):
        first = d["files"][0]
        d["label"] = "Main timeline" if first.parent == project else f"Timeline {first.parent.name[:8]}"
    return out


# ------------------------------------------------------------------ the editor

def op(fn):
    """Wrap an edit: undoable, rolled back on error, marks the project unsaved."""
    @functools.wraps(fn)
    def wrap(self, *args, coalesce=None, **kwargs):
        snap = self._snapshot()
        try:
            result = fn(self, *args, **kwargs)
            self._tidy()
        except Exception:
            self._restore(snap)
            raise
        now = time.time()
        merged = coalesce and coalesce == self._last_key and now - self._last_time < 1.5
        if not merged:
            self.undo_stack.append(snap)
            del self.undo_stack[:-UNDO_LIMIT]
        self._last_key, self._last_time = coalesce, now
        self.redo_stack.clear()
        self.dirty = True
        return result
    return wrap


class Editor:
    def __init__(self, project, doc_index=0, settings=None):
        self.project = Path(project)
        if self.project.is_file():
            self.project = self.project.parent
        if not self.project.is_dir():
            raise EditError(f"Folder not found: {self.project}")
        self.settings = dict(settings or load_settings())
        docs = load_documents(self.project)
        if not docs:
            raise EditError("No readable draft_content.json in this folder. "
                            "The project may be encrypted by this CapCut version.")
        self.doc_labels = [d["label"] for d in docs]
        self.doc_index = max(0, min(doc_index, len(docs) - 1))
        doc = docs[self.doc_index]
        self.files, self.hashes, self.data = doc["files"], doc["hashes"], doc["data"]
        self.locks, self.groups = set(), {}
        self.undo_stack, self.redo_stack = [], []
        self._last_key, self._last_time = None, 0.0
        self.dirty = False
        self.clipboard = None
        self._load_state()
        # Glow needs a real effect from CapCut to copy. Remember the first one seen.
        self._glow_saved = load_glow_template()
        here = next((e for e in self._materials().get("effects") or [] if is_glow(e)), None)
        if here is not None and self._glow_saved is None:
            self._glow_saved = copy.deepcopy(here)
            save_glow_template(self._glow_saved)
        # CapCut seems to store each track's position in its clips; keep that up
        # to date only if this project really follows the pattern.
        self._tri_is_index = all(
            s.get("track_render_index") == i
            for i, t in enumerate(self.data["tracks"]) if isinstance(t, dict)
            for s in t.get("segments", []) if isinstance(s, dict) and "track_render_index" in s)

    # ---------------------------------------------------------------- lookups

    @property
    def frame(self):
        fps = self.data.get("fps")
        return 1_000_000 / fps if isinstance(fps, (int, float)) and fps > 0 else 1_000_000 / 30

    def _materials(self):
        m = self.data.get("materials")
        return m if isinstance(m, dict) else {}

    def _text_tracks(self):
        return [t for t in self.data["tracks"] if isinstance(t, dict) and t.get("type") == "text"
                and isinstance(t.get("segments"), list)]

    def _texts(self):
        return {m.get("id"): m for m in self._materials().get("texts", []) if isinstance(m, dict)}

    def _mat_index(self):
        index = {}
        for lst in self._materials().values():
            if isinstance(lst, list):
                for obj in lst:
                    if isinstance(obj, dict) and isinstance(obj.get("id"), str):
                        index[obj["id"]] = (lst, obj)
        return index

    def _segs(self):
        """{segment id: (track, segment)} for every usable text clip."""
        return {s["id"]: (t, s) for t in self._text_tracks() for s in t["segments"]
                if isinstance(s, dict) and isinstance(s.get("id"), str) and has_time(s)}

    def _pick(self, ids, segs=None):
        segs = segs if segs is not None else self._segs()
        return set(segs) if ids is None else set(ids) & set(segs)

    def _group_of(self, seg_id):
        for gid, g in self.groups.items():
            if seg_id in g["ids"]:
                return gid
        return None

    @staticmethod
    def _pos(seg):
        tf = (seg.get("clip") or {}).get("transform") or {}
        return float(tf.get("x", 0.0)), float(tf.get("y", 0.0))

    @staticmethod
    def _set_pos(seg, x, y):
        clip = seg.setdefault("clip", {})
        clip["transform"] = {"x": float(x), "y": float(y)}

    def _snap(self, t):
        return int(round(round(t / self.frame) * self.frame))

    # ---------------------------------------------------------------- undo

    def _snapshot(self):
        return (dumps(self.data), set(self.locks), copy.deepcopy(self.groups))

    def _restore(self, snap):
        self.data = json.loads(snap[0])
        self.locks, self.groups = set(snap[1]), copy.deepcopy(snap[2])

    def undo(self):
        if self.undo_stack:
            self.redo_stack.append(self._snapshot())
            self._restore(self.undo_stack.pop())
            self.dirty, self._last_key = True, None

    def redo(self):
        if self.redo_stack:
            self.undo_stack.append(self._snapshot())
            self._restore(self.redo_stack.pop())
            self.dirty, self._last_key = True, None

    # ---------------------------------------------------------------- housekeeping

    def _tidy(self):
        """Keep the draft consistent after an edit."""
        tracks = self.data["tracks"]
        # CapCut removes empty tracks itself; do the same for text tracks.
        tracks[:] = [t for t in tracks if not (isinstance(t, dict) and t.get("type") == "text"
                                               and t.get("segments") == [])]
        prev = None
        for i, t in enumerate(tracks):
            if not isinstance(t, dict) or not isinstance(t.get("segments"), list):
                continue
            if t.get("type") == "text":
                segs = [s for s in t["segments"] if has_time(s)]
                t["segments"].sort(key=lambda s: seg_start(s) if has_time(s) else 0)
                # later text tracks must render above earlier ones
                vals = [s["render_index"] for s in segs if isinstance(s.get("render_index"), int)]
                want = min(vals) if vals else 14000
                if prev is not None and want <= prev:
                    want = prev + 1
                for s in segs:
                    if "render_index" in s:
                        s["render_index"] = want
                prev = want
            if self._tri_is_index:
                for s in t["segments"]:
                    if isinstance(s, dict) and "track_render_index" in s:
                        s["track_render_index"] = i
        segs = self._segs()
        self.locks &= set(segs)
        for gid in list(self.groups):
            if sum(1 for i in self.groups[gid]["ids"] if i in segs) < 2:
                del self.groups[gid]
        end = max((seg_end(s) for t in tracks if isinstance(t, dict)
                   for s in t.get("segments", []) if has_time(s)), default=0)
        if isinstance(self.data.get("duration"), int) and end > self.data["duration"]:
            self.data["duration"] = end

    def _free(self, track, start, end, ignore=()):
        return not any(s.get("id") not in ignore and seg_start(s) < end and seg_end(s) > start
                       for s in track["segments"] if has_time(s))

    def _new_text_track(self, like):
        track = {k: copy.deepcopy(v) for k, v in like.items() if k != "segments"}
        track.update(id=new_id(), segments=[])
        if "name" in track:
            track["name"] = ""
        tracks = self.data["tracks"]
        last = max(i for i, t in enumerate(tracks) if isinstance(t, dict) and t.get("type") == "text")
        tracks.insert(last + 1, track)
        return track

    @staticmethod
    def _move_to_track(seg, src, dst):
        if src is not dst:
            src["segments"].remove(seg)
            dst["segments"].append(seg)

    def _drop_materials(self, ids):
        """Delete materials that no clip uses any more."""
        used = set()
        for t in self.data["tracks"]:
            for s in (t.get("segments", []) if isinstance(t, dict) else []):
                if isinstance(s, dict):
                    used.add(s.get("material_id"))
                    used.update(s.get("extra_material_refs") or [])
        index = self._mat_index()
        for mid in set(ids) - used:
            if mid in index:
                lst, obj = index[mid]
                lst[:] = [o for o in lst if o is not obj]

    # ---------------------------------------------------------------- what the UI shows

    def _glow_of(self, seg, index):
        for ref in seg.get("extra_material_refs") or []:
            if ref in index and is_glow(index[ref][1]):
                return index[ref][1]
        return None

    def _glow_template(self):
        here = next((e for e in self._materials().get("effects") or [] if is_glow(e)), None)
        return here or self._glow_saved

    def _style_view(self, content, mat, seg, index):
        styles = content.get("styles") or [{}]
        st = styles[0] if isinstance(styles[0], dict) else {}
        solid = ((st.get("fill") or {}).get("content") or {}).get("solid") or {}
        out = {"font": (st.get("font") or {}).get("path", ""),
               "size": st.get("size", 15),
               "color": rgb_hex(solid.get("color", [1, 1, 1])),
               "stroke": {"on": False, "color": "#000000", "width": 40},
               "shadow": {"on": False, "color": "#000000", "alpha": 90, "blur": 15,
                          "distance": 5, "angle": -45},
               "background": {"on": False, "color": "#000000", "alpha": 100, "radius": 30},
               "curve": {"on": False, "angle": 0},
               "glow": {"on": False, "color": "#FFFFFF", "strength": 30, "range": 20, "name": ""}}
        flag = mat.get("check_flag") if isinstance(mat.get("check_flag"), int) else 7
        if flag & 16 and mat.get("background_style"):
            out["background"] = {"on": True, "color": mat.get("background_color") or "#000000",
                                 "alpha": round(float(mat.get("background_alpha", 1.0)) * 100, 1),
                                 "radius": round(float(mat.get("background_round_radius", 0.0)) * 100, 1)}
        curve = mat.get("text_curve")
        if isinstance(curve, dict) and curve.get("enable"):
            out["curve"] = {"on": True, "angle": float(curve.get("angle", 0))}
        glow = self._glow_of(seg, index)
        if glow is not None:
            bp = glow.get("bloom_params") or {}
            out["glow"] = {"on": True, "color": bp.get("color") or "#FFFFFF",
                           "strength": round(float(bp.get("strength", 0.3)) * 100, 1),
                           "range": round(float(bp.get("range", 0.2)) * 100, 1),
                           "name": glow.get("name", "")}
        strokes = st.get("strokes")
        if isinstance(strokes, list) and strokes and isinstance(strokes[0], dict):
            s = strokes[0]
            out["stroke"] = {"on": True,
                             "color": rgb_hex(((s.get("content") or {}).get("solid") or {}).get("color", [0, 0, 0])),
                             "width": round(float(s.get("width", 0.08)) / 0.2 * 100, 1)}
        shadows = st.get("shadows")
        if isinstance(shadows, list) and shadows and isinstance(shadows[0], dict):
            s = shadows[0]
            out["shadow"] = {"on": True,
                             "color": rgb_hex(((s.get("content") or {}).get("solid") or {}).get("color", [0, 0, 0])),
                             "alpha": round(float(s.get("alpha", 0.9)) * 100, 1),
                             "blur": round(float(s.get("diffuse", 0.025)) * 600, 1),
                             "distance": float(s.get("distance", 5)),
                             "angle": float(s.get("angle", -45))}
        known = {"fill", "font", "size", "range", "strokes", "shadows", "bold", "italic",
                 "underline", "useLetterColor"}
        out["extra"] = sorted(k for k in st if k not in known)
        return out

    def view(self):
        texts, index = self._texts(), self._mat_index()
        cfg = self.data.get("canvas_config") or {}
        tracks = []
        for t in self._text_tracks():
            clips = []
            for s in t["segments"]:
                if not (isinstance(s, dict) and has_time(s)):
                    continue
                mat = texts.get(s.get("material_id")) or {}
                content = read_content(mat)
                if content is None:
                    continue
                x, y = self._pos(s)
                clip = s.get("clip") or {}
                clips.append({"id": s["id"], "start": seg_start(s),
                              "dur": s["target_timerange"]["duration"],
                              "text": content["text"], "x": x, "y": y,
                              "scale": float((clip.get("scale") or {}).get("y", 1.0)),
                              "rot": float(clip.get("rotation", 0.0)),
                              "locked": s["id"] in self.locks,
                              "group": self._group_of(s["id"]),
                              "style": self._style_view(content, mat, s, index)})
            tracks.append({"id": t.get("id"), "clips": clips})
        ends = [c["start"] + c["dur"] for t in tracks for c in t["clips"]]
        return {"project": self.project.name, "path": str(self.project),
                "docs": self.doc_labels, "doc": self.doc_index,
                "canvas": {"w": cfg.get("width") or 1920, "h": cfg.get("height") or 1080},
                "fps": 1_000_000 / self.frame,
                "duration": max([self.data.get("duration") or 0] + ends),
                "tracks": tracks, "settings": self.settings, "dirty": self.dirty,
                "can_undo": bool(self.undo_stack), "can_redo": bool(self.redo_stack),
                "has_clipboard": self.clipboard is not None,
                "glow_ready": self._glow_template() is not None}

    # ---------------------------------------------------------------- split

    @op
    def split(self, ids=None):
        """One clip per word, back to back. Returns how many clips were split."""
        texts, index = self._texts(), self._mat_index()
        text_list = self._materials().get("texts", [])
        chosen = self._pick(ids) - self.locks
        old_ids, count, new_ids = set(), 0, []
        for track in self._text_tracks():
            out = []
            for seg in track["segments"]:
                mat = texts.get(seg.get("material_id")) if isinstance(seg, dict) else None
                content = read_content(mat) if mat else None
                parts = split_content(content) if content else []
                if (not isinstance(seg, dict) or seg.get("id") not in chosen or len(parts) < 2
                        or seg["target_timerange"]["duration"] < len(parts)):
                    out.append(seg)
                    continue
                tr, full_text = seg["target_timerange"], content["text"]
                n = len(parts)
                cuts = [tr["start"] + tr["duration"] * i // n for i in range(n + 1)]
                snapped = [cuts[0]] + [self._snap(c) for c in cuts[1:-1]] + [cuts[-1]]
                if all(b > a for a, b in zip(snapped, snapped[1:])):
                    cuts = snapped
                for i, (word, content_str) in enumerate(parts):
                    dur = cuts[i + 1] - cuts[i]
                    nmat = copy.deepcopy(mat)
                    nmat.update(id=new_id(), content=content_str)
                    for k, v in nmat.items():  # other fields holding a plain copy of the text
                        if k != "content" and isinstance(v, str) and v == full_text:
                            nmat[k] = word
                    text_list.append(nmat)
                    nseg = copy.deepcopy(seg)
                    nseg.update(id=new_id(), material_id=nmat["id"],
                                target_timerange=dict(tr, start=cuts[i], duration=dur))
                    if isinstance(nseg.get("source_timerange"), dict):
                        nseg["source_timerange"]["duration"] = dur
                    refs = []
                    for ref in seg.get("extra_material_refs") or []:
                        if ref in index:
                            lst, obj = index[ref]
                            c = copy.deepcopy(obj)
                            c["id"] = new_id()
                            fit_animations(c, dur)
                            lst.append(c)
                            refs.append(c["id"])
                        else:
                            refs.append(ref)
                    nseg["extra_material_refs"] = refs
                    out.append(nseg)
                    new_ids.append(nseg["id"])
                old_ids.add(mat["id"])
                old_ids.update(r for r in seg.get("extra_material_refs") or [] if r in index)
                count += 1
            track["segments"] = out
        self._drop_materials(old_ids)
        return {"count": count, "ids": new_ids}

    # ---------------------------------------------------------------- layering

    def _line_step(self, seg, size):
        """(sideways, downward) step between stacked words, in transform units."""
        cfg = self.data.get("canvas_config") or {}
        w, h = cfg.get("width") or 1920, cfg.get("height") or 1080
        scale = float(((seg.get("clip") or {}).get("scale") or {}).get("y", 1.0))
        px = float(size) * self.settings["preview_scale"] * min(w, h) / 1080 * scale
        return (self.settings["shift_x"] * px / (w / 2),
                self.settings["gap"] * px * 1.2 / (h / 2))

    def _layout_group(self, group, segs):
        """Place a group's unlocked words around the group's base position."""
        ids = group["ids"]
        first = next((segs[i][1] for i in ids if i in segs), None)
        if first is None:
            return
        content = read_content(self._texts().get(first.get("material_id")) or {}) or {}
        size = ((content.get("styles") or [{}])[0] or {}).get("size", 15)
        dx, dy = self._line_step(first, size)
        bx, by = group["base"]
        mid = (len(ids) - 1) / 2
        for i, sid in enumerate(ids):
            if sid in segs and sid not in self.locks:
                if self.settings["auto_offset"]:
                    self._set_pos(segs[sid][1], bx + (i - mid) * dx, by + (mid - i) * dy)
                else:
                    self._set_pos(segs[sid][1], bx, by)

    @op
    def layer(self, ids=None):
        """
        Stack runs of short words. Each word still appears at its own time but
        stays until the last word of its group ends, on its own track.
        """
        texts = self._texts()
        segs = self._segs()
        chosen = self._pick(ids, segs) - self.locks
        made = 0

        def short_word(seg):
            if seg.get("id") not in chosen or self._group_of(seg["id"]):
                return False
            content = read_content(texts.get(seg.get("material_id")) or {})
            words = content["text"].split() if content else []
            return len(words) == 1 and 0 < letters(words[0]) <= self.settings["short_max"]

        for base in list(self._text_tracks()):
            ordered = sorted((s for s in base["segments"] if has_time(s)), key=seg_start)
            runs, run = [], []
            for s in ordered:
                touching = run and abs(seg_start(s) - seg_end(run[-1])) <= self.frame * 1.5
                if short_word(s) and (not run or touching):
                    run.append(s)
                    continue
                runs.append(run)
                run = [s] if short_word(s) else []
            runs.append(run)

            for run in runs:
                if len(run) < 2:
                    continue
                pos = 0
                for size in chunk_sizes(len(run), max(2, int(self.settings["group_max"]))):
                    members = run[pos:pos + size]
                    pos += size
                    if len(members) < 2:
                        continue
                    end = seg_end(members[-1])
                    group = {"ids": [m["id"] for m in members], "base": list(self._pos(members[0])),
                             "track": base.get("id"), "ends": {m["id"]: seg_end(m) for m in members}}
                    for m in members:
                        m["target_timerange"]["duration"] = end - seg_start(m)
                    # every later word goes one track higher than the word before it
                    tracks = self.data["tracks"]
                    at = tracks.index(base)
                    for m in members[1:]:
                        target = None
                        for t in tracks[at + 1:]:
                            if (isinstance(t, dict) and t.get("type") == "text"
                                    and self._free(t, seg_start(m), end)):
                                target = t
                                break
                        if target is None:
                            target = self._new_text_track(base)
                        self._move_to_track(m, base, target)
                        at = tracks.index(target)
                    gid = new_id()[:8]
                    self.groups[gid] = group
                    self._layout_group(group, self._segs())
                    made += 1
        return {"count": made}

    @op
    def unlayer(self, ids=None):
        """Undo layering: words go back to back on their original track."""
        segs = self._segs()
        chosen = self._pick(ids, segs)
        count = 0
        for gid, g in list(self.groups.items()):
            if not chosen & set(g["ids"]):
                continue
            base = next((t for t in self._text_tracks() if t.get("id") == g["track"]), None)
            free = [sid for sid in g["ids"] if sid in segs and sid not in self.locks]
            for sid in free:  # first give every word its own time span back
                seg = segs[sid][1]
                end = g.get("ends", {}).get(sid, 0)
                if end > seg_start(seg):
                    seg["target_timerange"]["duration"] = end - seg_start(seg)
                self._set_pos(seg, *g["base"])
            for sid in free:  # then return it to the original track if there is room
                track, seg = segs[sid]
                if base is not None and self._free(base, seg_start(seg), seg_end(seg), ignore=(sid,)):
                    self._move_to_track(seg, track, base)
            del self.groups[gid]
            count += 1
        return {"count": count}

    @op
    def relayout(self):
        """Re-place every group using the current offset settings."""
        segs = self._segs()
        for g in self.groups.values():
            self._layout_group(g, segs)
        return {"count": len(self.groups)}

    def set_settings(self, patch):
        layout_before = [self.settings[k] for k in ("auto_offset", "gap", "shift_x", "preview_scale")]
        for k, v in (patch or {}).items():
            if k in DEFAULT_SETTINGS:
                kind = type(DEFAULT_SETTINGS[k])
                self.settings[k] = bool(v) if kind is bool else kind(v)
        self.settings["short_max"] = max(1, min(20, self.settings["short_max"]))
        self.settings["group_max"] = max(2, min(8, self.settings["group_max"]))
        self.settings["preview_scale"] = max(0.5, min(30.0, self.settings["preview_scale"]))
        save_settings(self.settings)
        if self.groups and layout_before != [self.settings[k] for k in
                                             ("auto_offset", "gap", "shift_x", "preview_scale")]:
            self.relayout(coalesce="relayout")

    # ---------------------------------------------------------------- manual edits

    @op
    def set_time(self, seg_id, start=None, dur=None, track_id=None):
        """Move or trim one clip. track_id may be another text track or "new"."""
        segs = self._segs()
        if seg_id not in segs:
            raise EditError("That clip no longer exists.")
        track, seg = segs[seg_id]
        tr = seg["target_timerange"]
        start = max(0, int(tr["start"] if start is None else start))
        dur = max(int(self.frame), int(tr["duration"] if dur is None else dur))
        if track_id == "new":
            target = self._new_text_track(track)
        elif track_id in (None, track.get("id")):
            target = track
        else:
            target = next((t for t in self._text_tracks() if t.get("id") == track_id), None)
            if target is None:
                raise EditError("That track no longer exists.")
        if not self._free(target, start, start + dur, ignore=(seg_id,)):
            raise EditError("Clips on the same track cannot overlap.")
        tr["start"], tr["duration"] = start, dur
        if isinstance(seg.get("source_timerange"), dict):
            seg["source_timerange"]["duration"] = dur
        self._move_to_track(seg, track, target)

    @op
    def shift_time(self, ids, delta):
        """Slide several clips together along the timeline."""
        segs = self._segs()
        moving = self._pick(ids, segs)
        delta = int(delta)
        for sid in moving:
            track, seg = segs[sid]
            if seg_start(seg) + delta < 0:
                raise EditError("Cannot move before the start of the timeline.")
        for sid in moving:
            segs[sid][1]["target_timerange"]["start"] += delta
        for sid in moving:
            track, seg = segs[sid]
            if not self._free(track, seg_start(seg), seg_end(seg), ignore=moving):
                raise EditError("Clips on the same track cannot overlap.")

    @op
    def nudge_pos(self, ids, dx, dy):
        """Move clips on screen."""
        segs = self._segs()
        for sid in self._pick(ids, segs):
            x, y = self._pos(segs[sid][1])
            self._set_pos(segs[sid][1], x + float(dx), y + float(dy))

    @op
    def set_text(self, seg_id, text):
        segs = self._segs()
        text = str(text).strip()
        if seg_id not in segs or not text:
            raise EditError("Text cannot be empty.")
        mat = self._texts().get(segs[seg_id][1].get("material_id"))
        content = read_content(mat) if mat else None
        if content is None:
            raise EditError("This clip's text cannot be edited here.")
        old = content["text"]
        styles = [s for s in content.get("styles") or [] if valid_range(s)]
        measure = detect_unit(old, styles)
        content["text"] = text
        if styles:
            styles[0]["range"] = [0, measure(text)]
            content["styles"] = [styles[0]]
        mat["content"] = dumps(content)
        for k, v in mat.items():
            if k != "content" and isinstance(v, str) and v == old and old:
                mat[k] = text

    @op
    def delete(self, ids):
        segs = self._segs()
        gone = set()
        for sid in self._pick(ids, segs):
            track, seg = segs[sid]
            track["segments"].remove(seg)
            gone.add(seg.get("material_id"))
            gone.update(seg.get("extra_material_refs") or [])
        self._drop_materials(gone)

    @op
    def lock(self, ids, value=None):
        """Lock or unlock clips. With no value: unlock if all are locked, else lock."""
        chosen = self._pick(ids)
        if value is None:
            value = not (chosen and chosen <= self.locks)
        self.locks = (self.locks | chosen) if value else (self.locks - chosen)
        return {"locked": bool(value), "count": len(chosen)}

    # ---------------------------------------------------------------- style

    def _write_style(self, mat, patch):
        content = read_content(mat)
        if content is None:
            return False
        styles = content.get("styles")
        if not isinstance(styles, list) or not styles:
            styles = content["styles"] = [{"range": [0, UNITS[1](content["text"])]}]
        flag = mat.get("check_flag") if isinstance(mat.get("check_flag"), int) else 7
        for st in styles:
            if not isinstance(st, dict):
                continue
            if "color" in patch:
                fill = st.setdefault("fill", {})
                fc = fill.setdefault("content", {})
                fc.setdefault("render_type", "solid")
                fc["solid"] = dict(fc.get("solid") or {}, color=hex_rgb(patch["color"]))
            if "size" in patch:
                st["size"] = max(1.0, min(500.0, float(patch["size"])))
                if st["size"] == int(st["size"]):
                    st["size"] = int(st["size"])
            if "font" in patch and patch["font"]:
                st["font"] = {"path": str(patch["font"]).replace("\\", "/"), "id": ""}
            if "stroke" in patch:
                s = patch["stroke"]
                if s.get("on"):
                    old = st.get("strokes")
                    stroke = dict(old[0]) if isinstance(old, list) and old and isinstance(old[0], dict) else {}
                    stroke["content"] = {"render_type": "solid", "solid": {"color": hex_rgb(s.get("color", "#000000"))}}
                    stroke["width"] = max(0.0, min(100.0, float(s.get("width", 40)))) / 100 * 0.2
                    stroke.setdefault("mode", 0)
                    st["strokes"] = [stroke]
                else:
                    st.pop("strokes", None)
            if "shadow" in patch:
                s = patch["shadow"]
                if s.get("on"):
                    old = st.get("shadows")
                    shadow = dict(old[0]) if isinstance(old, list) and old and isinstance(old[0], dict) else {
                        "thickness_projection_angle": -45, "thickness_projection_enable": False,
                        "thickness_projection_distance": 0}
                    shadow.update(
                        diffuse=max(0.0, min(100.0, float(s.get("blur", 15)))) / 100 / 6,
                        alpha=max(0.0, min(100.0, float(s.get("alpha", 90)))) / 100,
                        distance=max(0.0, min(100.0, float(s.get("distance", 5)))),
                        content={"render_type": "solid", "solid": {"color": hex_rgb(s.get("color", "#000000"))}},
                        angle=max(-180.0, min(180.0, float(s.get("angle", -45)))))
                    st["shadows"] = [shadow]
                else:
                    st.pop("shadows", None)
        mat["content"] = dumps(content)
        # CapCut also keeps a plain copy of the main settings on the material.
        if "color" in patch:
            mat["text_color"] = patch["color"].upper()
        if "size" in patch:
            mat["font_size"] = float(styles[0].get("size", 15))
        if "font" in patch and patch["font"]:
            mat["font_path"] = str(patch["font"]).replace("\\", "/")
            for k in ("font_id", "font_resource_id", "font_name"):
                if k in mat:
                    mat[k] = ""
        if "stroke" in patch:
            s = patch["stroke"]
            flag = (flag | 8) if s.get("on") else (flag & ~8)
            if s.get("on"):
                mat["border_color"] = s.get("color", "#000000").upper()
                mat["border_width"] = float(s.get("width", 40)) / 100 * 0.2
                mat["border_alpha"] = 1.0
            else:
                mat["border_color"] = ""
        if "shadow" in patch:
            s = patch["shadow"]
            flag = (flag | 32) if s.get("on") else (flag & ~32)
            mat["has_shadow"] = bool(s.get("on"))
            if s.get("on"):
                mat["shadow_color"] = s.get("color", "#000000").upper()
                mat["shadow_alpha"] = float(s.get("alpha", 90)) / 100
                mat["shadow_smoothing"] = float(s.get("blur", 15)) / 100 * 3
                mat["shadow_distance"] = float(s.get("distance", 5))
                mat["shadow_angle"] = float(s.get("angle", -45))
        if "background" in patch:  # stored on the material only
            b = patch["background"]
            if b.get("on"):
                if not mat.get("background_style"):
                    mat["background_style"] = 2
                mat["background_color"] = str(b.get("color", "#000000")).upper()
                hex_rgb(mat["background_color"])
                mat["background_alpha"] = max(0.0, min(100.0, float(b.get("alpha", 100)))) / 100
                mat["background_round_radius"] = max(0.0, min(100.0, float(b.get("radius", 30)))) / 100
                flag |= 16
            else:
                mat["background_style"], mat["background_color"] = 0, ""
                flag &= ~16
        if "curve" in patch:
            c = patch["curve"]
            on = bool(c.get("on"))
            mat["text_curve"] = {"enable": on, "angle": max(-360.0, min(360.0, float(c.get("angle", 0)))) if on else 0.0}
            flag = (flag | 128) if on else (flag & ~128)
        if "check_flag" in mat or flag != 7:
            mat["check_flag"] = flag
        return True

    def _write_glow(self, seg, mat, g, index):
        """Glow is an effect material linked from the clip, plus a flag on the text."""
        cur = self._glow_of(seg, index)
        flag = mat.get("check_flag") if isinstance(mat.get("check_flag"), int) else 7
        if g.get("on"):
            if cur is None:
                template = g.get("effect") or self._glow_template()
                if template is None:
                    raise EditError("Glow needs one example first: add glow to any text clip in CapCut, "
                                    "save, and open that project here. After that it is remembered.")
                cur = copy.deepcopy(template)
                cur["id"] = new_id()
                effects = self._materials().setdefault("effects", [])
                effects.append(cur)
                seg.setdefault("extra_material_refs", []).append(cur["id"])
                index[cur["id"]] = (effects, cur)
            bp = cur.setdefault("bloom_params", {})
            if "color" in g:
                hex_rgb(g["color"])
                bp["color"] = str(g["color"]).upper()
            if "strength" in g:
                bp["strength"] = max(0.0, min(100.0, float(g["strength"]))) / 100
            if "range" in g:
                bp["range"] = max(0.0, min(100.0, float(g["range"]))) / 100
            mat["check_flag"] = flag | 64
        else:
            if cur is not None:
                seg["extra_material_refs"].remove(cur["id"])
                index.pop(cur["id"], None)
                self._drop_materials([cur["id"]])
            if flag & 64:
                mat["check_flag"] = flag & ~64

    @op
    def style(self, ids, patch):
        """Change font, size, color or effects. Locked clips are skipped."""
        segs, texts, index = self._segs(), self._texts(), self._mat_index()
        done = 0
        for sid in self._pick(ids, segs) - self.locks:
            mat = texts.get(segs[sid][1].get("material_id"))
            if mat and self._write_style(mat, patch):
                if "glow" in patch:
                    g = {k: v for k, v in patch["glow"].items() if k != "effect"}
                    self._write_glow(segs[sid][1], mat, g, index)
                done += 1
        if "size" in patch and self.groups:
            for g in self.groups.values():
                self._layout_group(g, segs)
        return {"count": done}

    def copy_style(self, seg_id):
        segs = self._segs()
        mat = self._texts().get(segs[seg_id][1].get("material_id")) if seg_id in segs else None
        content = read_content(mat) if mat else None
        if content is None or not content.get("styles"):
            raise EditError("Select one text clip to copy its style.")
        self.clipboard = {
            "style": {k: copy.deepcopy(v) for k, v in content["styles"][0].items() if k != "range"},
            "fields": {k: copy.deepcopy(v) for k, v in mat.items() if k not in NOT_STYLE},
            "glow": copy.deepcopy(self._glow_of(segs[seg_id][1], self._mat_index()))}

    @op
    def paste_style(self, ids):
        """
        Give clips the copied clip's whole look, including effects this tool has
        no controls for (glow, presets and so on). Locked clips are skipped.
        """
        if not self.clipboard:
            raise EditError("Copy a clip's style first.")
        segs, texts, index = self._segs(), self._texts(), self._mat_index()
        done = 0
        for sid in self._pick(ids, segs) - self.locks:
            mat = texts.get(segs[sid][1].get("material_id"))
            content = read_content(mat) if mat else None
            if content is None:
                continue
            styles = [s for s in content.get("styles") or [] if isinstance(s, dict)]
            if not styles:
                styles = [{"range": [0, UNITS[1](content["text"])]}]
            content["styles"] = [dict(copy.deepcopy(self.clipboard["style"]),
                                      **({"range": s["range"]} if "range" in s else {}))
                                 for s in styles]
            mat["content"] = dumps(content)
            mat.update(copy.deepcopy(self.clipboard["fields"]))
            self._write_glow(segs[sid][1], mat, {"on": False}, index)
            if self.clipboard.get("glow"):
                self._write_glow(segs[sid][1], mat, {"on": True, "effect": self.clipboard["glow"]}, index)
            done += 1
        for g in self.groups.values():
            self._layout_group(g, segs)
        return {"count": done}

    # ---------------------------------------------------------------- disk

    def _state_path(self):
        return self.project / STATE_NAME

    def _load_state(self):
        try:
            state = json.loads(self._state_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        segs = self._segs()
        self.locks = {i for i in state.get("locks", []) if i in segs}
        for gid, g in (state.get("groups") or {}).items():
            if (isinstance(g, dict) and isinstance(g.get("ids"), list)
                    and isinstance(g.get("base"), list) and len(g["base"]) == 2
                    and sum(1 for i in g["ids"] if i in segs) >= 2):
                self.groups[gid] = {"ids": g["ids"], "base": g["base"],
                                    "track": g.get("track"), "ends": g.get("ends") or {}}

    def changed_on_disk(self):
        for f in self.files:
            try:
                if hashlib.sha256(f.read_bytes()).hexdigest() != self.hashes.get(str(f)):
                    return True
            except OSError:
                return True
        return False

    def backup(self):
        dest_root = self.project / BACKUP_DIR / datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        n = 1
        while dest_root.exists():
            n += 1
            dest_root = dest_root.with_name(dest_root.name.split("_v")[0] + f"_v{n}")
        for f in find_draft_files(self.project) + [self._state_path()]:
            if f.is_file():
                dest = dest_root / f.relative_to(self.project)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dest)
        return dest_root

    def save(self, force=False, open_ok=False):
        if not open_ok and capcut_running():
            raise EditError("CAPCUT: CapCut is open.")
        if not force and self.changed_on_disk():
            raise EditError("CHANGED: The project was changed outside this window since it was opened.")
        backup_dir = self.backup()
        text = dumps(self.data)
        for f in self.files:
            tmp = f.with_name(f.name + ".tmp_wordlayers")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, f)
            self.hashes[str(f)] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        self._state_path().write_text(json.dumps(
            {"locks": sorted(self.locks), "groups": self.groups}, indent=1), encoding="utf-8")
        self.dirty = False
        return {"backup": backup_dir.name, "files": len(self.files)}


def restore_latest_backup(project):
    """Put the newest backup back. Returns the backup's name."""
    project = Path(project)
    if capcut_running():
        raise EditError("CapCut is open. Close it completely, then restore again.")
    root = project / BACKUP_DIR
    backups = sorted((d for d in root.iterdir() if d.is_dir()),
                     key=lambda d: d.stat().st_mtime) if root.is_dir() else []
    if not backups:
        raise EditError("No backups found for this project.")
    latest = backups[-1]
    had_state = False
    for f in latest.rglob("*"):
        if f.is_file():
            target = project / f.relative_to(latest)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)
            had_state = had_state or f.name == STATE_NAME
    if not had_state:
        (project / STATE_NAME).unlink(missing_ok=True)
    return latest.name
