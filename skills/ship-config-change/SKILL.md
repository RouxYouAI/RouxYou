---
name: ship-config-change
description: How to ship a config change (watchtower interval, threshold, model param) safely via the /propose flow. Use when the user wants to adjust an operational parameter.
---

# Ship Config Change

## When to use

A config value needs adjustment — watchtower interval, a threshold, a model parameter, a polling period. Anything that lives in `state/*.json` and is hot-loaded by a service.

## Steps

1. **Verify current value.** Read the relevant `state/<service>.json` (e.g., `state/watchtower.json`) to confirm what's actually loaded. Don't guess.
2. **Draft proposal** via dashboard `/propose` flow:
   - `category: config`
   - Required fields: `{minutes, changed_by, reasoning}` (or service-specific equivalent)
   - EVIDENCE must cite real paths (path verifier will catch fabrications)
3. **Shadow + resolver + bigbrain.** Roux's shadow runs first; bigbrain reviews if governance-relevant.
4. **Executor handles dispatch.** `proposal_executor._handle_config` updates state file, signals the service.
5. **Verify-after.** Post-apply check confirms new value loaded (file mtime + value match).

## Common failures

- **Empty payload shape.** Old `_handle_config` sent `{interval_seconds}` but watchtower expects `{minutes, changed_by, reasoning}`. Match the schema or the executor rejects.
- **Module cache.** If watchtower edits land but behavior doesn't change, clear `__pycache__` + restart.
- **Path fabrication in EVIDENCE.** Pre-PROPOSE path verifier strips bogus citations; a redraft is forced. Cite real files.

## Files involved

- `state/<service>.json` — current value
- `shared/proposal_executor.py::_handle_config` — dispatcher
- `shared/authoring.py::validate_evidence_citations` — pre-PROPOSE verifier
