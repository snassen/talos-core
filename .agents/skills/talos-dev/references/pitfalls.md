# Pitfalls that have already bitten

- **Redraws jumped to the top.** A full `render()` from a timer loses the scroll position. Use `render(true)`,
  and poll with `pollWhileRefreshing`, which redraws once, when the answer is in.
- **The answer-key test slices app.js.** New code between `// ---- the answer key` and `const RENDER` must
  not open messages. Put new views above that section.
- **`self` direction.** Mail between the owner's own addresses is `self`. The Received and Sent filters both include
  it; other code that counts "received" should think about it too.
- **Gmail SEARCH MODSEQ over a huge backlog** makes Gmail answer "System Error" and drop the connection. The
  search runs in windows (`MODSEQ_ONE_SEARCH`, `MODSEQ_WINDOW` in `sources/gmail.py`).
- **Phrase search for binder terms.** A plain Messages search for "Check Point" matches check and point
  anywhere. Binder terms use `phraseto_tsquery` plus subject and sender ILIKE (`space._term_sql`). Default
  terms are the binder's name. For descriptive names ("FortiDLP Renewal") set real terms.
- **Values from anyone but the owner are model values.** Claude's answer-key labels propagate as
  source_kind `model` (`claude-gold:<set>:<pos>`), never as the owner's decisions (`gold.source_for`).
- **The `kind` dimension in tests.** `test_structure` has removed the `kind` dimension before, breaking later
  tests. If a test fails only in the full run, suspect a fixture that changes shared dimensions.
  `taxonomy_loaded`'s teardown now deletes committed values of a dimension the migrations lack before it drops
  the dimension (a Studio test that committed kind values hit it).
- **Launchd's last exit code** belongs to the previous run. Judge a running job by its pid.
- **Merge conflicts** usually land in app.py imports, style.css, conftest `TABLES`, test_guards, and migration
  numbers. Rebuild the file from HEAD plus the branch's hunks rather than hand-merging blindly.
- **Heavy PDFs from the UI.** Card shadows and rounded clips become full-page raster masks in Chrome's print.
  The field guide's snapshots turn shadows off and clip square (`~/TalosData/manual/frames.py`).
- **Opening compose makes a draft.** Discard drafts that tests or screenshots created.
- **The door in tests.** `app.create(..., allowed_hosts=["testserver"])` without an `auth_store` leaves the
  door open for the older API tests. Any other app is shut. Tests of the door or of production-like apps
  pass `auth_store=webauth.MemoryStore()` and sign in, or mint a session (`webauth.mint`).
- **Guard wording.** test_guards greps for words like EXPUNGE or `\Deleted` in the source. Harmless UI text
  that mentions them fails too, so reword it.
