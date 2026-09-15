# Implementation and validation reports

Reports record what was implemented or tested **on their stated dates**. Start with
[current project status](../PROJECT_STATUS.md) for today's source-level scope and
[the runbook](../README.md) for executable setup instructions.

| Report | Date | Scope |
|---|---|---|
| [INF-01](INF-01_IMPLEMENTATION.md) | 2026-09-10 | Reference target, bundles, catalog and fixture Docker acceptance |
| [INF-02](INF-02_IMPLEMENTATION.md) | 2026-09-10 | Isolated target inference with a scripted provider |
| [INF-03](INF-03_IMPLEMENTATION.md) | 2026-09-10 | Three intervention surfaces and delivery receipts |
| [INF-04–INF-12](INF-04_TO_INF-12_IMPLEMENTATION.md) | 2026-09-10 | Research integration, SDK/UI, live infrastructure and recovery acceptance |
| [MVP alignment](MVP_ALIGNMENT_REPORT.md) / [MVP validation](MVP_VALIDATION_REPORT.md) | 2026-09-01 | Historical platform baseline before the reference target and research integration |

The September 1 statements that target agents were absent and Docker/PostgreSQL
validation was deferred apply to that baseline. The September 10 work added one
finance reference target and recorded live Docker/PostgreSQL/MinIO validation.
Real-model acceptance, held-out benchmark improvement, actual checkpoint loading,
clean network builds and Firecracker validation remain unproven in these reports.

Saved evidence includes the [infrastructure JUnit results](validation/INF-04-12_BACKEND_JUNIT.xml)
and [fixture acceptance records](validation/INF-04-12_ACCEPTANCE.json).
The adjacent MVP JSON reports, [original acceptance output](acceptance-validation.json),
duplicate MVP reports under `backend/`, and root `SOURCE_SHA256SUMS.txt` are archived
artifacts. Preserve their original dates, results and hashes. The
[interactive-guide validation](validation/project-guide/result.json) concerns the
guide's browser behavior, not live platform or model acceptance.
