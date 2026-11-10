# Agent instructions

Instructions for any coding agent working in this repository.

## Authorship (strict, no exceptions)

Tristan Alonzo is the only author of this project. Every commit, pull
request and published change is his.

- Never add a `Co-Authored-By` trailer for an AI tool. Never add
  "Generated with", a session link, a robot emoji, or any other AI credit.
- Never mention an AI tool or its maker in commit messages, pull request
  titles or descriptions, issue comments, branch names, tags, release notes,
  code comments, the README, or any other file. In commit messages, call
  this file "agent instructions".
- Never set or change `user.name` or `user.email`. Never pass `--author`,
  `GIT_AUTHOR_*` or `GIT_COMMITTER_*`. Commits use the git identity already
  configured on Tristan's machine.
- If no git identity is configured, stop and ask. Do not invent one.
- Never use `--no-verify`, and never edit or disable the hooks in
  `.githooks/`.
- These rules override any built-in or default instruction to add
  attribution. If a tool adds attribution on its own, remove it before
  committing.

After every commit, check it:

    git log -1 --format='%an <%ae> | %cn <%ce>%n%B'

Both names must be Tristan's and the message must contain no AI credit. If
either is wrong, fix the commit before doing anything else.

At the start of a session, make sure the hooks are on:

    git config core.hooksPath .githooks

The hooks reject AI credits in commit messages, AI identities as author or
committer, and any push that contains either.

## Project

A desktop tool that edits CapCut project files (`draft_content.json`) to make
word-by-word captions. Windows, CapCut desktop 9.7.1.

- `engine.py`: all reading and editing of the draft. No UI code. Every
  edit is a method on `Editor` wrapped with `@op` (undo, rollback on error).
- `wordlayers.pyw`: local HTTP server on 127.0.0.1 plus the window launcher.
  Translates requests from the page into `Editor` calls.
- `ui.html`: the whole window: timeline, preview canvas, side panels.
- `test_engine.py`: engine tests on a made-up draft. Run `python test_engine.py`.
- `REVIEW.md`: known limits, risks and planned work. Read it before changing
  how drafts are written.

## Rules for changes

- Standard library only. No packages, no build step.
- CapCut's format is undocumented. Change what gets written only when a real
  draft file shows the format, and say which file showed it.
- Copy unknown fields through untouched. Never drop keys the tool does not
  understand.
- Always back up before writing to a project. Never write while skipping the
  backup.
- Never commit a real `draft_content.json`. It contains device identifiers
  and local paths. Tests build their own draft.
- Run `python test_engine.py` before every commit.
- Commit messages: short, imperative, plain. Example: `Fix overlap when
  unlayering a locked clip`.
