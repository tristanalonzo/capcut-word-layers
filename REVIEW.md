# Review: known limits, risks and next steps

An honest list of what this tool does not handle yet. Items are ordered by
how likely they are to cause trouble on a real edit.

Checked against CapCut desktop 9.7.1 on Windows. "Confirmed" means a real
draft file showed the format. "Assumed" means it is a best guess.

## 1. What is confirmed and what is assumed

| Area | Status | Notes |
| --- | --- | --- |
| Splitting a clip into word clips | Confirmed in CapCut | Opened and displayed correctly |
| Stroke, shadow, background, curve | Format confirmed | Written output matches a CapCut-made file; not yet opened in CapCut |
| Glow | Format confirmed | Copied from a real glow effect; not yet opened in CapCut |
| New text tracks for layers | Format confirmed | `track_render_index` follows track order in a real 3-track file |
| Font change by file path | Assumed | CapCut's own fonts may also need `font_id` / `font_resource_id` |
| Preview text size | Assumed | Size-to-pixel factor is a guess; calibrate once |
| Saving while CapCut is on its home screen | Assumed | Expected to re-read on open; not tested |
| `Timelines` copies | Assumed | Treated as mirrors of the main file when ids match |
| Background style number | Assumed | Only style `2` has been seen |
| Curve range | Assumed | Slider allows -360 to 360; real limits unknown |

First job after publishing: open one project in CapCut for each "not yet
opened" row and record the result here.

## 2. Possible bugs

### Draft files

- **Other project files are not updated.** Only `draft_content.json` (and
  its `Timelines` copies) is written. `draft_meta_info.json`,
  `draft_content.json.bak`, `template-2.tmp` and cover files keep old data
  such as duration. CapCut has not complained so far, but it is unverified.
- **Mirror files are overwritten with the main file's content.** If a
  `Timelines` copy differs from the main file in small ways, those
  differences are lost on save.
- **A mirror that differs in its clip ids is treated as a separate
  timeline.** The user then edits only one of them, and it is unclear which
  one CapCut loads.
- **`_tidy` renumbers `render_index` on every text track after any edit,**
  including tracks the user never touched. CapCut itself leaves stale values
  there, so this is probably harmless, but it is more change than needed.
- **Empty text tracks are removed after every edit.** CapCut does the same,
  but a user could have left one on purpose.
- **Project duration only grows.** It is never shortened after deleting or
  trimming clips.
- **Save is not atomic across files.** Each file is replaced safely, but a
  crash between files leaves the main file and its mirrors out of step. The
  backup still allows recovery.

### Splitting

- **Keyframes are not handled.** A clip with keyframes (`common_keyframes`,
  `keyframe_refs`) is copied to every word with the original keyframe times,
  which will be wrong.
- **Time is divided evenly.** Word length and the audio are ignored. Clips
  from auto captions carry real word timings (`words.start_time`), which are
  currently ignored.
- **Words are found by spaces.** Chinese, Japanese and Thai text is not
  split. Punctuation stays attached to its word. A line break counts as a
  space, so multi-line layouts are flattened.
- **Style range units are detected by matching the end of the last range.**
  Text with emoji or other non-ASCII characters and several style ranges
  could pick the wrong unit.
- **Text templates, bubbles and effect presets** are copied as they are.
  Nothing checks that they still make sense on a single word.
- **Animations longer than a word clip are shortened in proportion.** Loop
  animations are not adjusted.

### Layering

- **Groups ignore meaning.** Runs are chosen only by word length and
  adjacency, so a group can straddle the end of one sentence and the start
  of the next.
- **Adjacent means within 1.5 frames.** Clips with a slightly larger gap are
  never grouped.
- **Line spacing uses the first word's font size.** Groups with mixed sizes
  will be spaced unevenly.
- **Group memory lives in a side file keyed by clip id**
  (`_wordlayers_state.json`). If clips are split, deleted or re-created in
  CapCut, locks and groups can silently disappear or point at nothing.
- **Unlayer restores the end times saved at layering.** If a clip was
  trimmed by hand afterwards, unlayer undoes that trim.
- **Words are stacked top to bottom only.** Right-to-left or side-by-side
  layouts need manual placement.

### Styles

- **A style change is written to every style range in the clip.** Per-letter
  colours inside one clip are flattened.
- **Editing a clip's text keeps only its first style range.**
- **Paste style copies almost every field of the text material,** including
  fixed width, alignment and curve. That is intended, but it can surprise.
- **Glow depends on a file in CapCut's cache folder** on this computer. If
  the cache is cleared, or the project moves to another PC, the glow may not
  load.
- **Only one glow type is remembered,** the first one the tool ever sees.
- **Glow, background and shadow offsets are not exposed.**
- **Font list:** `.ttc` collections are skipped, the scan stops at 1,500
  fonts, and names come from a minimal font-file reader that can fail on
  unusual fonts (it then falls back to the file name).

### Window

- **Unsaved work is lost if the window is closed and the confirmation is
  accepted.** The server stops a few seconds later. There is no autosave.
- **The whole timeline is rebuilt after every edit.** Fine for hundreds of
  clips; likely slow for several thousand.
- **Undo keeps up to 50 full copies of the draft in memory.** Large projects
  (tens of MB) will use a lot of memory and make each edit slower.
- **Settings changes are not part of undo,** although the re-layout they
  trigger is.
- **After an undo back to the saved state the project still shows as
  unsaved.**
- **Dragging moves one clip at a time.** Several clips can only be moved
  together with the arrow keys.
- **Two windows on the same project** will overwrite each other. Nothing
  prevents opening a second one.
- **Preview gaps:** no video frame, no curve, no letter spacing, no
  automatic line wrapping, no animation. Stroke, shadow and glow sizes are
  approximate.

## 3. Edge cases not yet tested

- Vertical 1080x1920 projects (text size and offsets were only seen at
  1920x1080).
- Projects with video, audio, stickers and effects tracks between text
  tracks.
- Clips with speed changes, or a `source_timerange` that is not null.
- Rotated or scaled text in layered groups.
- Very short clips where a word would get less than one frame.
- A project with more than one timeline.
- Text with only punctuation, only emoji, or leading and trailing spaces.
- Project folders with non-English names or very long paths.
- Drafts stored on OneDrive or another synced folder (file locks, sync
  conflicts with the backup folder).
- Drop-frame frame rates (29.97, 59.94) and frame snapping.
- A draft that CapCut is writing at the moment the tool reads it.
- Python started from the Microsoft Store (the `.pyw` association can
  differ).
- A computer with neither Edge nor Chrome (falls back to the default
  browser, which adds tabs and an address bar).

## 4. Security and privacy

- **The window is a local web page.** The server listens on `127.0.0.1` on a
  random port. Every request needs a random token, and the `Host` header is
  checked to stop other websites reaching it.
- **The token is in the page address.** It can end up in browser history.
  It is only valid while the app is running, but moving it out of the
  address would be cleaner.
- **The font endpoint reads files by path.** It is limited to font file
  types and local drives; network paths are refused, because opening one
  could leak Windows login hashes to a remote machine. It can still read any
  font-typed file on the computer. Limiting it to known font folders would
  be tighter.
- **Draft files are treated as data, never as code.** Still, a draft from
  someone else is untrusted input: its font paths and text reach the page.
  Clip text is inserted as text, not HTML. Keep it that way.
- **"Open" accepts any folder path** and will write a backup folder and
  state file there when saving.
- **Draft files contain personal data:** a device id, a hashed MAC address
  and the Windows user name inside paths. Never commit a real
  `draft_content.json` or a backup folder. `.gitignore` already excludes
  them; test fixtures must be made up.
- **No network access.** The tool never contacts the internet and has no
  third-party packages, so there is no supply-chain exposure beyond Python
  itself.
- **Backups pile up inside the project folder** and are never pruned. They
  can be uploaded by cloud sync or shared along with the project by
  accident.

## 5. Real-world use

- **CapCut updates itself.** The draft format can change without notice,
  and some versions encrypt drafts. Read `platform.app_version` on open and
  warn when the version has not been tested.
- **This is unofficial.** Editing project files is not something CapCut
  supports. Check CapCut's terms before promoting the tool, and keep the
  "not affiliated" note in the README.
- **Pro effects stay Pro.** Copying a Pro glow onto more clips does not
  bypass CapCut's export rules.
- **Client work:** always try a new feature on a duplicate of the project
  first. Backups protect the draft file, not media or other project files.
- **Windows only for now.** The project folder location, the process check
  and the window launcher are Windows-specific. macOS needs its own paths.
- **Installing Python is a barrier** for other editors. A packaged `.exe`
  removes it.
- **No licence file yet.** Pick one before others use or contribute.
- **The name includes "CapCut".** That is a trademark; a neutral name avoids
  takedown trouble.

## 6. Improvements, roughly in order of value

1. **Use real word timings** when a clip has them (auto captions), instead
   of dividing time evenly.
2. **Version check on open,** with a clear warning for untested CapCut
   versions.
3. **Sentence-aware grouping:** never group across `.`, `?`, `!` or a long
   pause.
4. **Reload when the project file changes on disk,** to shorten the loop
   with CapCut.
5. **Emphasis rules:** make long or chosen words larger or a different
   colour automatically.
6. **Layering presets** that save a whole set of settings and styles by
   name.
7. **Multi-clip drag, ripple move and visible snap guides** in the timeline.
8. **Backup pruning** (keep the last N) and a backup list to restore from.
9. **Autosave of unsaved edits** to a recovery file.
10. **Curve and letter spacing in the preview;** a still video frame as the
    preview background.
11. **Keyframe support** when splitting.
12. **Tests that drive the window,** plus a Windows run in GitHub Actions.
13. **Split `engine.py` into modules** (files, text, layering, style) and
    add type hints.
14. **Packaging** as a single `.exe` so Python is not required.
15. **Smaller undo snapshots** (store only what changed).
