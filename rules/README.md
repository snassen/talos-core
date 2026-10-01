# Rules

Rules as data, saved with `uv run talos rules load rules/<file>.json` (idempotent: a second load
changes nothing; a rule's version goes up only when its conditions or action change) and applied with
`uv run talos rules run`. Re-running is safe: rule-made values are recomputed, human decisions are never
touched. `talos rules remove ID` (or `--all [--keep ID,…]`) removes rules and the values they made; each
removed rule is kept in the `rule_removed` table with when and why (`talos rules removed`).

- `example-rules.json`: an example rule set in the shape of his researched rules (twenty rules, one
  dimension each, the reasoning and evidence in each description), with invented senders and hosts. His own
  rules, and the report that explains them, live outside the repository: `TALOS_HOME/config/rules/` and
  `TALOS_HOME/docs/` (talos.personal).
- `accounts.example.json`: the shape of `TALOS_HOME/config/accounts.json`, his accounts and addresses.
- `taxonomy.json`: the closed value lists (`talos taxonomy load`).
- `structure.json`: the mailbox structure (docs/mailbox-structure-plan.md): per account, the ordered
  targets under `Talos/` and their conditions over effective values; first match wins. Read by
  `talos structure plan`; bump `version` when you change it. Nothing in it writes to a mailbox.
