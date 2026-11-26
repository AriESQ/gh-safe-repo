# Immutable releases: live API probe (2026-11-26)

Evidence for #127 (`gh_safe_repo/plugins/releases.py`). The probe ran on two
throwaway repos created for it on the AriESQ account, which is on the Free
plan (see `docs/2026-11-01_security-settings-availability.md`): one public,
one private. gh 2.105.0, REST API version 2022-11-28.

## Results

Both repos gave identical answers, in this order:

| # | Call | Status | Body |
|---|---|---|---|
| 1 | `GET …/immutable-releases` (fresh repo) | 200 | `{"enabled":false,"enforced_by_owner":false}` |
| 2 | `PUT …/immutable-releases` (immediately after `POST /user/repos`) | 204 | |
| 3 | `PUT` again (after 2 s) | 204 | |
| 4 | `GET` | 200 | `{"enabled":true,"enforced_by_owner":false}` |
| 5 | `DELETE …/immutable-releases` | 204 | |
| 6 | `GET` | 200 | `{"enabled":false,"enforced_by_owner":false}` |
| 7 | `PUT` | 204 | |
| 8 | `GET` | 200 | `{"enabled":true,"enforced_by_owner":false}` |

## Findings

- **"Off" is `200 {"enabled": false}`, not the spec's 404.** The REST
  description documents 404 for a repo with immutable releases off. Live
  GitHub never returned 404, on a fresh repo or after `DELETE`. This is the
  same spec/live gap as `automated-security-fixes` (#94). The plugin already
  reads `enabled` from any 2xx, so it reads this as off; 404 is still
  accepted as off in case GitHub starts following the spec.
- **A Free private repo accepts the setting.** `PUT` gave 204 and `GET`
  confirmed it on, matching the docs (`fpt: '*'`). No plan-based `SKIP` is
  needed.
- **No eventual-consistency retry needed** for a `PUT` sent immediately after
  `POST /user/repos` (one sample per visibility).
- **`PUT` is idempotent:** a second `PUT` on an enabled repo is 204, not 409.
- `enforced_by_owner` was `false` throughout; user-owned repos have no
  organization policy. The `true` case is still untested (see #9 for org
  probes).

## Method

The probe script made the calls above with `gh api -i` and recorded the status
line and body. It could not delete the repos afterwards because the token lacks
the `delete_repo` scope, so they were left for manual deletion.
