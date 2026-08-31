# Alpha Engine — demo recording
#
# Rendered with VHS (https://github.com/charmbracelet/vhs):
#     vhs demo/demo.tape
#
# Every command below is really executed and its real output recorded. Nothing
# here is a mockup, and the numbers are whatever the engine actually returns on
# the day it is run — including the ones that say it found nothing.

Output demo/alpha-engine-demo.mp4
Output demo/alpha-engine-demo.gif

Set Shell zsh
Set FontSize 15
Set Width 1500
Set Height 1100
Set Padding 25
Set Theme "Catppuccin Mocha"
Set TypingSpeed 28ms

# --- setup, hidden from the recording ---
Hide
Type "source .venv/bin/activate"
Enter
# Without this, zsh runs '#' as a command and every narration line prints
# 'command not found: #' over the demo.
Type "setopt interactive_comments"
Enter
# Drop the cached BTC series so the scan below performs a real network fetch
# rather than showing '[cache] using cached BTC' under a claim of live data.
Type "rm -f data/cache/price/BTC_1d.json"
Enter
Type "clear"
Enter
Sleep 3s
Show

Type "# Alpha Engine — a deterministic research engine for market signals."
Enter
Sleep 1500ms
Type "# It is a MEASURING INSTRUMENT, not a forecaster. Watch what that means."
Enter
Sleep 2500ms
Type "clear"
Enter
Sleep 500ms

# ---------------------------------------------------------------- 1. a signal
Type "# 1. Generate a signal. This fetches live prices — no API key needed."
Enter
Sleep 2s
Type "python -m alpha_engine.cli.main scan BTC --no-record | head -32"
Enter
Sleep 9s
Type "clear"
Enter
Sleep 500ms

Type "# Every contributing opinion is shown with its weight. Nothing is hidden"
Enter
Type "# behind a single score, and the LLM never touches a number."
Enter
Sleep 3s
Type "clear"
Enter
Sleep 500ms

# ------------------------------------------------------------ 2. source health
Type "# 2. Scrapers die quietly. Every feed records its own health, so a dead"
Enter
Type "#    source looks different from a quiet one."
Enter
Sleep 2500ms
Type "python -m alpha_engine.cli.main health"
Enter
Sleep 7s
Type "clear"
Enter
Sleep 500ms

# ------------------------------------------------------------- 3. the track record
Type "# 3. The live track record, scored against what prices actually did."
Enter
Sleep 2s
Type "python -m alpha_engine.cli.main record-stats | head -20"
Enter
Sleep 8s
Type "# Note records_scored vs records_total. If too much of the record cannot"
Enter
Type "# be scored, the hit rate is withheld rather than quietly computed on a"
Enter
Type "# biased subset. A partial number wearing a whole one's name is worse"
Enter
Type "# than no number."
Enter
Sleep 4s
Type "clear"
Enter
Sleep 500ms

# ------------------------------------------------------------ 4. the noise floor
Type "# 4. 495 factors, ranked by how well each predicted forward returns."
Enter
Sleep 2s
Type "python -m alpha_engine.cli.main factors BTC --top 8"
Enter
Sleep 11s
Type "# Look at the vs_floor column, and at the noise floor underneath."
Enter
Sleep 3s
Type "clear"
Enter
Sleep 500ms

Type "# Score 495 factors on a little data and something WILL look brilliant."
Enter
Type "# That is arithmetic, not alpha. Each factor is measured against the |IC|"
Enter
Type "# the best of 495 RANDOM factors would reach on that factor's own sample."
Enter
Sleep 4s
Type "python -m alpha_engine.cli.main factors BTC --top 0 | grep -c ' noise$'"
Enter
Sleep 6s
Type "# That many of the 495 are indistinguishable from noise."
Enter
Sleep 3s
Type "clear"
Enter
Sleep 500ms

# ------------------------------------------------------------------ 5. the sweep
Type "# 5. The real test: does ANY of it survive out-of-sample?"
Enter
Sleep 2s
Type "# Rank factors on the first 60% of history, then re-measure the winners"
Enter
Type "# on the last 40% they have never seen — and run the whole procedure"
Enter
Type "# again on SHUFFLED returns, which carry no information by construction."
Enter
Sleep 4s
Type "python scripts/factor_sweep.py --permutations 1 | head -31"
Enter
Sleep 38s
Type "clear"
Enter
Sleep 500ms

Type "# The shuffled column is what this procedure produces from nothing."
Enter
Type "# Only the DIFFERENCE means anything — and it is negative."
Enter
Sleep 3s
Type "# The factor registry replicated no better than noise. No edge."
Enter
Sleep 3s
Type "clear"
Enter
Sleep 500ms

# ------------------------------------------------------------------- 6. closing
Type "# That is the whole point. The engine is built so the honest negative"
Enter
Type "# answer is the easy one to get, and it publishes that answer:"
Enter
Sleep 3s
Type "head -20 FINDINGS.md"
Enter
Sleep 9s
Type "clear"
Enter
Sleep 500ms

Type "# +0.0% directional edge over 6,788 signals. Measured, not claimed."
Enter
Sleep 2500ms
Type "# RESEARCH ONLY. Not financial advice."
Enter
Sleep 4s
