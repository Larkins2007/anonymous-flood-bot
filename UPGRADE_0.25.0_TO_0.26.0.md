# Upgrade 0.25.0 → 0.26.0

1. Back up the SQLite database.
2. Replace sources; keep `.env` and DB path.
3. On first start, additive migrations create `jf_member_events`, `jf_member_meta`, optional columns on `group_members`, and `jf_economy_equipped`.
4. No role mappings or member history are deleted.
5. `/syncroles` still never edits posts 40/41; it publishes two new list messages and reports each list status separately.
6. Economy: after buying, use `Надеть <предмет>` and `Витрина`.

No secrets are required beyond existing env vars.
