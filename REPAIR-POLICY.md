# Repair policy

A technical failure is not a scientific rejection.

The same author or reviewer receives the exact validation error and up to three correction attempts. Unfavorable grades and valid scientific objections must be preserved. Unknown charges are not retried automatically.

If repair fails, the runner writes `coordinator_handoffs/<job>.json` and reports `needs_coordinator_repair`. The candidate is retained. The main coordinator reads that packet and saved responses, then supplies `coordinator_fixes/<job>.json`:

```json
{
  "reason": "Explain the technical repair without altering scientific judgment",
  "output": {}
}
```

Replace the empty object with an output conforming to the schema in the handoff. Resume with the same run directory and unchanged configuration/profile. The same validators run again. Invalid coordinator fixes are not accepted.

Structural failures receive bounded writer/editor exchanges before the same escalation. A coordinator's structural edit must pass a fresh editor check before official review. Nothing may be waved through as a pass.

An exact quotation may have matching outer quotation delimiters; the quoted content still must match a supplied string. Missing, duplicated, unknown, or unverified warning criteria trigger repair before a scientific improvement decision can be used.

The main agent must actively monitor these handoffs. The files do not independently wake a coordinator model. Every paid attempt remains subject to the hard dollar and call caps. A reviewer must assess the proposed plan and anticipated findings rather than demand that future experiments have already happened.
