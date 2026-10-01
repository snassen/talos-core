"""The screen lab: measuring and training talos-doctor's screen on samples of prompt injection and ordinary text.

talos-doctor (its own product, beside this repository) screens a change before an agent reads it: its
deterministic rules, and Jev. The lab is where those are measured and improved, with what Talos already has:
rules as data, versioned Jev runs with budgets, and the owner's decisions.

- sources.py   one module per source (public datasets, Talos's own code as ordinary text, generated canary
               attacks), each switched on or off, with a sample cap, pinned to the revision it was imported at
- corpus.py    one shape for every sample, and deduplication across all sources (exact and near)
- runs.py      a rules run (free) or a Jev run (paid, estimated first, the owner's go) over the samples
- report.py    catch rate and false-alarm rate per source, category, kind of text and rule; where the rules and
               Jev disagree, which is what new rules are mined from

Safety. Samples are attack text. They are handled by code and by Jev (which has no tools and answers only with
probabilities), stored in the database only, and never printed: every report shows counts, rates, rule ids and
categories, never a sample. Attacks the lab generates itself carry a harmless canary instead of a payload.
"""
