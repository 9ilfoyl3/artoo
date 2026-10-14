# Agent Note: global default agent preset

Status: implemented

## Problem

`agent_presets.is_default` already existed, but the Agent configuration page
did not expose a way to change it. The built-in "smart reasoning" preset was
therefore effectively fixed as the default. The existing write paths also
scoped default changes to the preset owner, while chat selection treated
`is_default` as a single available default. Historical data could consequently
contain zero or multiple defaults, and a platform super-admin could not replace
the built-in default with a platform-level custom preset.

## Decision

`is_default` is a single platform-wide default Agent preset:

- `PUT /api/agent-presets/{preset_id}/default` requires `require_platform`,
  clears the flag from every other preset, and sets the selected built-in or
  platform-level preset as the sole default.
- The Agent page shows a star action on every non-default card to platform
  super-admins. The current global default keeps the existing filled star and
  "default" badge.
- Member-facing create and update requests reject any attempt to change
  `is_default`; tenant-owned presets remain tenant configuration and cannot
  become the platform default.
- `_ensure_builtin_presets()` normalizes legacy state to exactly one default.
  It preserves the built-in smart-reasoning default when present, otherwise
  keeps the earliest existing default, and falls back to smart reasoning when
  no default exists.
- Deleting the current global default makes the built-in smart-reasoning preset
  the default again.

Changing a built-in preset's default status uses the dedicated endpoint rather
than the normal update endpoint, so built-in configuration remains immutable.

## Alternatives considered

**Keep one default per preset owner.** Rejected because the chat page selects
one visible default, so multiple owner-scoped defaults have ambiguous behavior
and can leave the platform default unchanged.

**Allow built-in presets to be updated through the normal edit endpoint.**
Rejected because default selection is a platform policy, while built-in
configuration is intentionally immutable and shared across tenants.

**Store a preset id in `PlatformConfig` instead of using `is_default`.**
Rejected because the existing `is_default` column already carries the
selection and is consumed by chat; this change fixes its scope without a
schema migration or a second source of truth.

## Consequences

The default selection is deterministic across tenants and survives preset
changes through a dedicated permission boundary. No database migration is
required, and legacy multi-default rows are normalized on the existing
built-in-preset ensure path. Tenant members lose the previously implemented
per-owner default API behavior, which was not exposed in the UI and conflicted
with chat selection. A tenant-owned preset cannot be made global because the
platform super-admin cannot see tenant-private presets.

## Testing

`../.venv/bin/pytest -q tests/test_agent_config_default.py` passes with 4 tests
covering selection of built-in and platform presets, legacy normalization,
member permission rejection, and deletion fallback. `npm run build` in
`frontend/` succeeds.
