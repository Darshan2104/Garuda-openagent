# Feature quality: remaining campaign work

Reviewed on 12 November 2026 against `a5e908fa390464b0b98347c899bb624e6160ab42`.
The [campaign tracker #355](https://github.com/Darshan2104/Garuda-openagent/issues/355)
and four implementation issues remained open. This dated snapshot distinguishes
merged mechanisms from proposed follow-ups. A linked PR is not a completion or
merge claim. The [original campaign plan](../plans/2026-10-26-feature-quality-campaign.md)
retains its initial scope and sequencing.

| Issue | Already merged at the reviewed base | Proposed follow-up |
| --- | --- | --- |
| [#363: shared spend](https://github.com/Darshan2104/Garuda-openagent/issues/363) | [#408](https://github.com/Darshan2104/Garuda-openagent/pull/408) added native tree admission; later changes added failed-call usage, resume inheritance and consult sharing | [#453](https://github.com/Darshan2104/Garuda-openagent/pull/453) makes collection attempts inherit the active root gate, preventing dispatch after root exhaustion and charging worker usage to the same total |
| [#370: investigation context](https://github.com/Darshan2104/Garuda-openagent/issues/370) | [#417](https://github.com/Darshan2104/Garuda-openagent/pull/417) added typed read-only investigation reports; [#435](https://github.com/Darshan2104/Garuda-openagent/pull/435) added context-only forks with fresh identity/admission and stale inherited evidence | [#451](https://github.com/Darshan2104/Garuda-openagent/pull/451) adds read-only fork preview through the execution preparation owner, explicitly leaving admission unchecked |
| [#384: editing workers](https://github.com/Darshan2104/Garuda-openagent/issues/384) | [#434](https://github.com/Darshan2104/Garuda-openagent/pull/434) added sequential local workers with independent repositories/ownership and checked integration; [#447](https://github.com/Darshan2104/Garuda-openagent/pull/447) added conservative inventory/retirement/recovery | [#450](https://github.com/Darshan2104/Garuda-openagent/pull/450) closes child environment services after interruption while retaining uncertain ownership |
| [#389: live native output](https://github.com/Darshan2104/Garuda-openagent/issues/389) | [#432](https://github.com/Darshan2104/Garuda-openagent/pull/432) added shared loop accumulation; [#444](https://github.com/Darshan2104/Garuda-openagent/pull/444) wired terminal runs; [#448](https://github.com/Darshan2104/Garuda-openagent/pull/448) wired SDK callbacks | [#452](https://github.com/Darshan2104/Garuda-openagent/pull/452) wires dashboard chat previews without persisting deltas or duplicating completed output |

## Remaining acceptance work

The four follow-ups are reviewable increments. Keep the parent issues open until
their complete acceptance criteria and exact merged revisions are verified.

- **Spend:** dollar limits remain stops based on reported spend, rather than
  prepaid exact maxima. Preserve explicit unknown failed/cancelled usage and
  crash/resume reservation provenance; a passing normal-resume case does not
  establish crash accounting. External ACP model calls remain outside native
  spend enforcement.
- **Investigation:** preserve read-only collection briefs where sufficient.
  Preview is inspection, not a reservation: execution must independently
  revalidate the source and current workspace. Context rewind restores neither
  files nor processes, and historical checks never verify the new candidate.
- **Workers:** concurrent editing fan-out and non-local worker backends remain
  unsupported. Closing known environment services does not prove all descendant
  cleanup. Keep uncertain repositories, leases and capacity quarantined. Live
  Docker integration checks require separate evidence.
- **Streaming:** dashboard and SDK callbacks do not cover terminal interactive
  chat, auxiliary calls or child calls. Preserve known partial usage on
  interruption and distinguish transport failure from completed turns. No
  latency or quality improvement is inferred from tool overlap or fixture speed.

## Validation owners

Use `tests/test_collection_integration.py` and `tests/test_spend_gate.py` for root
admission/accounting; `tests/test_sessions_resume.py` and
`tests/test_investigation_scope.py` for fork and report boundaries;
`tests/test_editing_workers.py` for real repository/ownership lifecycles; and
`tests/test_web_live_runs.py` with `tests/test_loop_streaming.py` for dashboard
delivery and loop semantics. The existing
[live Chrome chat check](../development/browser-checks.md) exercises rendered
previews, completed responses, approvals and grounding without a paid provider.

Record focused checks, full-suite results, dependency/install conditions and
unavailable integrations in each PR. The campaign tracker is the rollup, not a
fifth runtime feature. Close it only after the remaining implementation issues
have their own acceptance evidence.
