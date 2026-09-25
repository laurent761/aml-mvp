# UI smoke test — 2026-09-25

The production Docker UI is running at http://localhost:3000 against the local backend.
Tested in headless Chromium at 1440×1000 and mobile navigation at 390×844.

## Passed

- Production build and Compose health checks.
- Navigation through all 11 sections, API connectivity, and initial data loading:
  no browser exceptions or HTTP errors during the navigation smoke test.
- Mobile menu navigation; no document-level horizontal overflow on the Targets page.
- Campaign budget acknowledgement prevents premature submission.
- Created a bounded task through the UI and launched a linear campaign with the
  explicit heuristic attacker configuration: one episode, two steps, seed 321.
- Campaign completed with two simulated calls, 124 simulated token units, and no
  model charges. Containment was VERIFIED with zero preflight violations.
- Campaign details displayed two public steps and eight trusted effects.
- Completed-episode evidence export returned HTTP 201; download returned HTTP 200
  with 34,030 bytes, matching the export's reported size.

Identifiers:

- Task: `task_9f35ff849c7e4ea99d7d318988145301`
- Campaign: `campaign_992c90e47b814ffd80f1ac6e26814393`
- Episode: `episode_82ed514d` (display prefix)
- Evidence: `artifact_10208182479740b9bfafcddda0817112`

## Usability findings — not fixed in this test

1. With registered targets but no ordinary attack campaigns, the overview says
   “Register your first target.” The empty-state guidance should reflect existing
   targets and direct the user to campaign creation.
2. The campaign episode panel enables “Export evidence” while an episode is still
   active. Clicking it returns HTTP 409: “evidence can only be exported for a
   terminal episode.” Disable or explain the action until the episode finishes.

## Scope and evidence

This was a scripted-fixture UI integration test, not a real-model experiment or a
full accessibility/security audit. No forbidden state was observed. Successful
exploit replay, cancellation, and the complete registration tour were not tested.
Managed research campaigns are filtered out of the ordinary campaign views;
their absence there is consistent with the current product scope.

Browser scripts, JSON reports, and screenshots are temporarily available under
`/private/tmp/aml-ui-browser/`, including `report.json`, `final.json`,
`overview.png`, `mobile.png`, and `campaign-final.png`.
The test task, campaign, and exported evidence remain in the local database.
