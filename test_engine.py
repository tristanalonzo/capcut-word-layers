"""Engine tests on a made-up draft. Run:  python test_engine.py"""
import json
import os
import tempfile
from pathlib import Path

os.environ["HOME"] = os.environ["USERPROFILE"] = tempfile.mkdtemp()  # keep settings out of the real home
import engine  # noqa: E402
from engine import EditError, Editor  # noqa: E402


def make_project(text="this is a test of incredible things", seconds=7):
    root = Path(tempfile.mkdtemp()) / "proj"
    (root / "Timelines" / "t1").mkdir(parents=True)
    content = {"text": text, "styles": [{"fill": {"content": {"render_type": "solid", "solid": {"color": [1, 1, 1]}}},
                                         "font": {"path": "C:/x/en.ttf", "id": ""}, "size": 15,
                                         "range": [0, len(text)]}]}
    draft = {
        "id": "D1", "fps": 30.0, "duration": seconds * 1_000_000,
        "canvas_config": {"width": 1920, "height": 1080},
        "tracks": [
            {"id": "V", "type": "video", "segments": []},
            {"id": "T", "type": "text", "flag": 0, "name": "", "segments": [{
                "id": "S1", "material_id": "M1", "extra_material_refs": ["A1"],
                "target_timerange": {"start": 0, "duration": seconds * 1_000_000}, "source_timerange": None,
                "clip": {"scale": {"x": 1.0, "y": 1.0}, "rotation": 0.0, "transform": {"x": 0.0, "y": 0.0}},
                "render_index": 14000, "track_render_index": 1}]}],
        "materials": {"texts": [{"id": "M1", "content": json.dumps(content), "check_flag": 7,
                                 "text_color": "#FFFFFF", "font_size": 15.0}],
                      "material_animations": [{"id": "A1", "type": "sticker_animation", "animations": []}]}}
    text_json = json.dumps(draft)
    (root / "draft_content.json").write_text(text_json)
    (root / "Timelines" / "t1" / "draft_content.json").write_text(text_json)
    return root


def clips(ed):
    return [(ti, c) for ti, t in enumerate(ed.view()["tracks"]) for c in t["clips"]]


def no_overlaps(ed):
    for t in ed.view()["tracks"]:
        spans = sorted((c["start"], c["start"] + c["dur"]) for c in t["clips"])
        assert all(a[1] <= b[0] for a, b in zip(spans, spans[1:])), spans


def test_split_and_layer():
    ed = Editor(make_project())
    assert ed.split()["count"] == 1
    words = [c["text"] for _, c in clips(ed)]
    assert words == "this is a test of incredible things".split()
    spans = [(c["start"], c["start"] + c["dur"]) for _, c in clips(ed)]
    assert spans[0][0] == 0 and spans[-1][1] == 7_000_000
    assert all(a[1] == b[0] for a, b in zip(spans, spans[1:]))          # back to back
    assert len({c["id"] for _, c in clips(ed)}) == 7
    assert len(ed.data["materials"]["texts"]) == 7 and len(ed.data["materials"]["material_animations"]) == 7

    ed.set_settings({"short_max": 4, "group_max": 4})
    assert ed.layer()["count"] == 2                                      # 5 short words -> 3 + 2
    by_text = {c["text"]: (ti, c) for ti, c in clips(ed)}
    end = lambda w: by_text[w][1]["start"] + by_text[w][1]["dur"]
    assert end("this") == end("is") == end("a")                          # held until the group ends
    assert [by_text[w][0] for w in ("this", "is", "a")] == [0, 1, 2]     # one track higher each
    assert by_text["incredible"][1]["group"] is None and by_text["incredible"][0] == 0
    assert by_text["this"][1]["y"] > by_text["is"][1]["y"] > by_text["a"][1]["y"]
    no_overlaps(ed)

    ed.unlayer()
    assert all(ti == 0 and c["y"] == 0 for ti, c in clips(ed))
    assert [(c["start"], c["start"] + c["dur"]) for _, c in clips(ed)] == spans
    no_overlaps(ed)


def test_locks_protect_clips():
    ed = Editor(make_project())
    ed.split()
    first = clips(ed)[0][1]["id"]
    ed.lock([first])
    ed.layer()
    assert clips(ed)[0][1]["group"] is None                              # locked word was left alone
    ed.style(None, {"color": "#FF0000", "size": 30})
    colors = {c["text"]: c["style"]["color"] for _, c in clips(ed)}
    assert colors["this"] == "#FFFFFF" and colors["is"] == "#FF0000"
    ed.set_settings({"gap": 2.0})
    ed.unlayer()
    no_overlaps(ed)


def test_style_effects_and_paste():
    ed = Editor(make_project("one two"))
    ed.split()
    a, b = [c["id"] for _, c in clips(ed)]
    ed.style([a], {"stroke": {"on": True, "color": "#00FF00", "width": 50},
                   "shadow": {"on": True, "color": "#000000", "alpha": 80, "blur": 30, "distance": 8, "angle": -45}})
    mat = ed._texts()[ed._segs()[a][1]["material_id"]]
    st = json.loads(mat["content"])["styles"][0]
    assert abs(st["strokes"][0]["width"] - 0.1) < 1e-9 and st["shadows"][0]["alpha"] == 0.8
    assert mat["check_flag"] == 7 | 8 | 32
    ed.copy_style(a)
    ed.paste_style([b])
    vb = dict((c["id"], c) for _, c in clips(ed))[b]
    assert vb["style"]["stroke"]["on"] and vb["style"]["shadow"]["on"] and vb["text"] == "two"
    ed.style([a], {"stroke": {"on": False}, "shadow": {"on": False}})
    assert ed._texts()[ed._segs()[a][1]["material_id"]]["check_flag"] == 7


def test_manual_edits_undo_and_save():
    root = make_project("one two three")
    original = (root / "draft_content.json").read_text()
    ed = Editor(root)
    ed.split()
    a, b, c = [x["id"] for _, x in clips(ed)]
    try:
        ed.set_time(b, start=0)
        raise AssertionError("overlap should be refused")
    except EditError:
        pass
    ed.set_time(b, track_id="new")
    assert len(ed.view()["tracks"]) == 2
    ed.undo()
    assert len(ed.view()["tracks"]) == 1
    ed.redo()
    ed.nudge_pos([b], 0.1, -0.2)
    ed.set_text(c, "3")
    ed.lock([a])
    saved = ed.save()
    assert saved["files"] == 2
    main = (root / "draft_content.json").read_text()
    assert main == (root / "Timelines" / "t1" / "draft_content.json").read_text() != original
    again = Editor(root)
    assert a in again.locks and {x["text"] for _, x in clips(again)} == {"one", "two", "3"}
    engine.restore_latest_backup(root)
    assert (root / "draft_content.json").read_text() == original
    assert not Editor(root).locks


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok  ", name)
