# Floppy Jellyfin Plugin — Design

## Problem

Floppy currently tracks Jellyfin playback via a generic webhook (Jellyfin's official or unofficial Webhook plugin posting to `/webhook/jellyfin/<token>/`). Jellyfin's `PlaybackProgress` notification fires on essentially every server-side progress report, with no built-in throttle or configurable interval in either webhook plugin. Server logs from a real session showed ~80 webhook calls in under 3 minutes for a single episode, saturating Floppy's single-concurrency `interactive` Celery worker (`CELERY_WORKER_CONCURRENCY = 1`, `config/settings.py:1108`) for the entire viewing session. A `Stop` event landing during that window queues behind whatever heartbeats are still in flight, which is the delay the user originally reported.

Raising worker concurrency was considered and rejected: the webhook processor deliberately treats `Play` (which covers both `PlaybackStart` and `PlaybackProgress`) as "not played" specifically to avoid a heartbeat racing the real `Stop` event and completing an entry early with the wrong `start_date` (see the comment in `integrations/webhooks/jellyfin.py:93-102`, and the fix in commit `83733b92`). Running heartbeat tasks concurrently would reintroduce that exact race at the process level. The fix has to reduce the volume of work, not parallelize it.

A pure Jellyfin-side config change (uncheck `Playback Progress` in the existing webhook plugin) was identified as a viable interim stopgap but is explicitly out of scope for this project per user decision — this design covers the plugin only.

## Decision

Build a dedicated Jellyfin server plugin ("Floppy") that talks directly to a new Floppy API endpoint, replacing the generic-webhook indirection for users who install it. It is a **new, third option** alongside the existing Official/Unofficial webhook setup instructions — those remain unchanged and continue to work for users who don't install the plugin.

### Scope (decided during brainstorming)

- **Audience**: built for other self-hosted Floppy users, not just the author's own server — implies a hosted plugin repository, defensive config handling, and versioning discipline against Jellyfin's plugin ABI.
- **Direction**: report-only (Jellyfin → Floppy). The existing "push Floppy watched-state back into Jellyfin" feature (`integrations/jellyfin_sync.py`, `push_jellyfin_watched`) is untouched and stays on its current polling-based mechanism.
- **Relationship to existing webhook setup**: additive. Webhook-based setup keeps working; the plugin is the new recommended path.
- **Event coverage**: full parity with the current webhook processor — playback events (start/progress/pause/stop) **and** MarkPlayed/MarkUnplayed — with each event category independently toggleable per destination (mirrors the existing per-user `jellyfin_mark_played_enabled`/`jellyfin_mark_unplayed_enabled` settings, just configured plugin-side).
- **Interim stopgap**: explicitly declined. No changes to the existing webhook path as part of this project.

## Prior art

[`jellyfin/jellyfin-plugin-trakt`](https://github.com/jellyfin/jellyfin-plugin-trakt) is the closest existing analog — an official Jellyfin-org plugin that scrobbles playback to an external per-user service. Its `ServerMediator.cs` and `Configuration/PluginConfiguration.cs` are reused directly as the basis for this design rather than inventing a new pattern:

- **Config shape**: a plain array of per-user entries (`TraktUser[] TraktUsers`, keyed by `LinkedMbUserId` — Jellyfin's internal user GUID) on a class extending the Jellyfin SDK's `BasePluginConfiguration`, with `AddUser`/`RemoveUser` helpers.
- **Event hooks**: `IHostedService` subscribing to `ISessionManager.PlaybackStart` / `PlaybackProgress` / `PlaybackStopped`, and `IUserDataManager.UserDataSaved` filtered to `UserDataSaveReason.TogglePlayed`.
- **Throttle mechanism**: not a fixed timer. Trakt keeps an in-memory `Dictionary<userGuid, PlaybackState{IsPaused, PlaybackPositionTicks, PlaybackTime}>` and on every `PlaybackProgress` event only sends if the pause state changed, or a seek/skip is detected (tick-position delta doesn't match elapsed wall-clock time, ±10s tolerance). During normal uninterrupted playback this is **zero** network calls between Start and Stop.
- **Completion signal**: `PlaybackStopEventArgs.PlayedToCompletion` — a direct boolean from Jellyfin itself, avoiding the heuristics the current webhook path needs (see the `_is_played` comment in `jellyfin.py`).

Floppy's own `app/live_playback.py:340` already interpolates the "now watching" position client-side from `(offset_seconds, updated_at_ts)` between updates — it was built for a sparse-update model, so adopting Trakt's zero-heartbeat-during-steady-playback approach doesn't degrade the live playback card.

## Architecture

```
Jellyfin Server (per household)
  └── Floppy Plugin (C#, installed via custom repo)
        ├── Session hooks: ISessionManager (PlaybackStart/Progress/Stopped)
        ├── Watch-state hook: IUserDataManager (UserDataSaved → mark played/unplayed)
        ├── Per-(user,item) state-change debounce (Trakt-style) + minimum-interval
        │     floor as a defensive cap (configurable, default 15s, hard-enforced
        │     minimum e.g. 5s so a misconfigured value can't hammer Floppy)
        ├── Config page: list of "Destinations"
        │     [Jellyfin user (LinkedMbUserId), Floppy base URL, API token,
        │      per-event-type toggles, throttle-floor seconds, enabled flag]
        └── HTTP POST → Floppy: new endpoint, X-API-Key auth, fire-and-forget

Floppy (Django)
  └── POST /api/jellyfin-plugin/event/  (DRF APIView, APIKeyAuthentication)
        └── JellyfinPluginEventProcessor
              → reuses existing _handle_movie / _process_tv
                (integrations/webhooks/base.py)
              → queued on existing `interactive` Celery queue,
                CELERY_TASK_PRIORITY_INTERACTIVE (config/settings.py:1163)
```

### Plugin-side components

- **Session listener** (`IHostedService`): subscribes to `PlaybackStart`/`PlaybackProgress`/`PlaybackStopped`. On `PlaybackProgress`, applies the Trakt-style debounce first (skip if pause state unchanged and no skip detected); if not skipped, additionally enforces the configurable minimum-interval floor (default 15s, hard-enforced minimum e.g. 5s) as a defensive cap against pathological cases (e.g. rapid scrubbing). `PlaybackStart`/`PlaybackStopped` always bypass both checks — rare, time-sensitive, must never be dropped or delayed (this is the exact class of event the recent `83733b92` fix cared about).
- **Watch-state listener**: subscribes to `UserDataSaved`, filtered to `UserDataSaveReason.TogglePlayed`, mapped to MarkPlayed/MarkUnplayed, gated per-destination by its own toggle.
- **Destinations config**: `FloppyDestination[]` on a `PluginConfiguration : BasePluginConfiguration`, one entry per `(Jellyfin user, Floppy account)` pair — supports multiple Floppy accounts against one shared Jellyfin server. Configured on the plugin's page in Jellyfin's admin dashboard.
- **HTTP sender**: `IHttpClientFactory`, short timeout (~5s), one retry, then log-and-drop. Never blocks Jellyfin's own playback/session pipeline. Per-destination try/catch (one destination's failure doesn't affect others), matching Trakt's per-user error isolation.

### Floppy-side changes

- New endpoint `POST /api/jellyfin-plugin/event/`, modeled directly on the existing `SubmitListensView` pattern (`api/listenbrainz_views.py`): DRF `APIView`, `authentication_classes = [APIKeyAuthentication]` (already exists — `X-API-Key` header, keyed on the same `User.token` already used for webhook URLs; no new auth system, no new user-mapping concept).
- New `JellyfinPluginEventProcessor` (`integrations/webhooks/jellyfin_plugin.py`), reusing `_handle_movie`/`_process_tv`/`_find_tv_media_id` from `base.py`, fed a clean native payload instead of parsed Jellyfin webhook JSON:
  - `event_type`: `play` | `pause` | `stop` | `mark_played` | `mark_unplayed`
  - `tmdb_id` / `imdb_id` / `tvdb_id`
  - `season` / `episode` (TV only)
  - `position_seconds` / `duration_seconds`
  - `played_to_completion` (bool, from Jellyfin's own `PlaybackStopEventArgs.PlayedToCompletion` — replaces the heuristic-based completion logic the webhook path needs)
- Same `interactive` Celery queue and priority as today — this design reduces the number of tasks landing there, it doesn't change the queueing model.

## Reliability

- Plugin: fire-and-forget async HTTP, short timeout, one retry, then drop with a log entry (Jellyfin's own log, visible in its dashboard). No unbounded in-plugin queueing — the plugin process isn't a durable queue.
- Floppy: standard DRF validation (400 on malformed payload), 401 via `APIKeyAuthentication` on invalid/missing token — same contract as every other ingest endpoint.

## Distribution & versioning

- Standard Jellyfin plugin repository manifest (`manifest.json`/`repo.json`) hosted as a static file, most simply via GitHub Pages — mirrors how the existing "Unofficial Repo" (shemanaev) is already documented in `templates/users/integrations.html:200-206`. Users add the repo URL once in Jellyfin's Plugin Catalog, then install "Floppy" like any other plugin.
- Jellyfin plugins declare a `TargetAbi` against specific server versions. Each Jellyfin major release may require a plugin release to stay compatible — an ongoing, recurring maintenance cost, same as the Trakt plugin carries. This is called out explicitly as a real commitment, not a one-time build.

## Testing

- Pure logic (debounce/skip-detection, payload mapping) as .NET unit tests — no running Jellyfin server required, mirrors how Trakt's `PlaybackState` comparison logic is unit-testable in isolation.
- Integration: a docker-compose Jellyfin dev instance + a manual verification checklist (play/pause/seek/stop/mark-played/mark-unplayed) run before each release. Full automated E2E of "Jellyfin loads a real plugin DLL" is not practical to script reliably.
- Floppy side: standard Django tests against the new endpoint and processor, following the existing webhook-processor test patterns.

## Out of scope (this design)

- Bidirectional sync / writeback from Floppy into Jellyfin via the plugin (existing `push_jellyfin_watched` mechanism stays as-is).
- Replacing or deprecating the existing webhook-based setup instructions.
- Any interim mitigation to the current webhook path (e.g. disabling `Playback Progress` in the existing webhook plugin config) — explicitly declined by the user for this project.
