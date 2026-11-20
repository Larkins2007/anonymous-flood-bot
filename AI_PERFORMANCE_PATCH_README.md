# Justice Faite AI performance patch

This package contains a reversible patcher for the audited 0.26.2 source tree and a corrected Termux launcher.

## Changes
- Caps conversational archive lookup at 1,200 rows instead of loading up to 25,000 rows into memory; old replied-to messages are still fetched directly.
- Reduces default live-context and completion budgets for the tablet CPU model.
- Adds a priority queue: direct user questions have priority 0; optional analytics has priority 10.
- Aligns deadlines: Render caller 300 seconds, tablet generation 240 seconds, job lease 600 seconds.
- Makes model-generated periodic analytics, semantic review, and rolling-memory compaction opt-in. Existing archive and saved memories are not deleted. Local deterministic Watch checks remain enabled.
- Fixes the broken `start_llama.sh` line continuations and uses moderate CPU batch sizes.
- Adds a queue-priority regression test.

## Apply
1. Make a copy of the repository or download the GitHub source ZIP.
2. Put `apply_justice_ai_optimizer.py` in the repository root.
3. Run `python apply_justice_ai_optimizer.py`.
4. The script checks the audited source fragments before changing anything, creates a timestamped `.justice-faite-backup-*` directory, applies updates and syntax-checks every root Python file.
5. Run:
   ```bash
   PYTHONDONTWRITEBYTECODE=1 python tests_bridge.py
   PYTHONDONTWRITEBYTECODE=1 python -m unittest -v test_ai_bridge_worker
   PYTHONDONTWRITEBYTECODE=1 python tests_release.py
   ```

If an expected source fragment is missing, the script changes nothing. The source has moved from the audited 0.26.2 baseline and needs a revision-specific patch.

## Rollout
- Keep `JF_AI_ENABLED=false` until the tablet model server and worker connect successfully.
- Then enable `JF_AI_ENABLED=true` in Render and test one short question.
- Do not delete the SQLite database, archive, saved memories, or secrets.
- The patch does not claim to resolve a Render OOM without inspecting actual service metrics.
- Only one model server and one worker process should run on the tablet.
