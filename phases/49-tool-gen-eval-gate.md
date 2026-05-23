# P-49 — Tool-gen eval gate

## Status

done.

## Outclass claim

**Auto-tools quarantined until 3 eval cases pass.** No tool reaches production without proving itself.

## Goal

Tool-gen is safe.

## Files

`sera/tools/genesis.py`, `sera/eval/tool_eval.py`.

## Verification

broken auto-tool stays quarantined.

## Dependencies

P-48, P-10.


## Notes

2026-05-23: sera/eval/tool_eval.py — ToolEvalCase (args, expect_substring/expect_regex/expect_not_error/timeout_s), ToolEvalVerdict (case_name, passed, reason, output), EvalReport (n_pass/n_fail/total/all_passed), PromotionResult. run_tool_eval(tool, cases) → invokes tool.handler with asyncio.wait_for; substring/regex match; expect_not_error=False inverts. promote_tool(name, cases, quarantine_dir, auto_dir, min_pass=3): registry lookup → eval → if n_pass≥min_pass move file from quarantine to auto, else file stays quarantined. is_quarantined()/is_promoted()/list_quarantined() helpers. sera/tools/genesis.py adds DEFAULT_QUARANTINE_DIR constant. Verification: broken tool (handler returns 'always wrong', cases expect 'right') → 0/3 pass → file stays in quarantine/, never reaches auto/. 24 tests, 981 total.
