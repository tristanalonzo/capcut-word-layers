# CapCut Word Layers

A small window for CapCut desktop projects that turns a text clip into
word-by-word captions: one clip per word, short words stacked into layers,
with a timeline you can edit by hand.

Built and tested against CapCut desktop 9.7.1 on Windows. CapCut's project
format is undocumented, so other versions may behave differently.

This is an unofficial tool. It is not affiliated with or endorsed by CapCut
or Bytedance.

## Run it

1. Install Python 3 from python.org (no extra packages needed).
2. Close CapCut.
3. Double-click `wordlayers.pyw`.
4. Pick a project, edit, press **Save**, then open CapCut.

Keep `wordlayers.pyw`, `engine.py` and `ui.html` in the same folder. The
folder can be anywhere.

Every save first copies the project files into `_wordlayers_backups` inside
the project folder. **Restore backup** puts the newest copy back.

## What it does

| Action | Key | Effect |
| --- | --- | --- |
| Split into words | `S` | One clip per word, back to back, same style and animation |
| Layer short words | `G` | Runs of short words stay on screen together, each on its own track |
| Unlayer | `U` | Puts a layered group back the way it was |
| Lock / unlock | `L` | Locked clips are skipped by every action; they show striped with a padlock |
| Copy / paste style | `Ctrl+Shift+C` / `V` | Copies a clip's whole look, including effects with no controls here |

Actions use the selected clips, or every clip when nothing is selected.

You can also drag clips to move or trim them, drag text in the preview to
place it, and change font, size, color and effects in the side panel.

## Good to know

- The preview shows text only, and sizes and effects are approximate. Use
  **Preview calibration** once to match CapCut's text size.
- Stroke, shadow, glow, background and curve have controls. Glow needs one
  example first: add glow to any text clip in CapCut once and open that
  project here. The tool remembers it for every project after that.
- CapCut does not re-read a project that is open in its editor, and it
  overwrites the file when it saves. Go back to CapCut's home screen (or close
  CapCut) before saving here.
- The window is a local page served from your own computer. Nothing leaves it.
- If the window does not open or something fails, see `wordlayers.log`.

## Development

    python test_engine.py

After cloning, turn on the repository's git hooks once:

    git config core.hooksPath .githooks

Known limits and planned work are in `REVIEW.md`.
