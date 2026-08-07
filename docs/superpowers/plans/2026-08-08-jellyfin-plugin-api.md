# Floppy Jellyfin Plugin — Floppy-side API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a new Floppy API endpoint that accepts clean, structured playback/watch-state events (the contract the future native Jellyfin plugin will call), reusing the existing movie/TV tracking logic instead of duplicating it.

**Architecture:** A new DRF `APIView` (`api/jellyfin_plugin_views.py`) authenticated with the existing `APIKeyAuthentication` (`X-API-Key` header, same `User.token` already used for webhook URLs) delegates to a new `JellyfinPluginEventProcessor` (`integrations/webhooks/jellyfin_plugin.py`) that subclasses `BaseWebhookProcessor` and reuses its existing `_process_media`/`_handle_movie`/`_process_tv`/`_handle_tv_episode` methods — the same tracking logic the current Jellyfin webhook already exercises, just fed a purpose-built payload shape instead of parsed Jellyfin webhook JSON.

**Tech Stack:** Django, Django REST Framework, existing `integrations.webhooks` processor pattern, `pytest`/Django `TestCase`.

## Global Constraints

- This plan is Floppy-side only. It does not touch the Jellyfin C# plugin (separate plan) or the existing webhook-based setup, which must keep working unchanged.
- Reuse `APIKeyAuthentication` (`api/authentication.py:64`) — do not add a new auth mechanism.
- Reuse `BaseWebhookProcessor`'s existing movie/TV handling (`integrations/webhooks/base.py`) — do not duplicate `_handle_movie`/`_handle_tv_episode` logic.
- Mock all `app.providers.tmdb.*` calls in tests — no real network calls in this plan's tests.
- Follow existing file-per-integration convention: dedicated processor file, dedicated view file (mirrors `integrations/webhooks/listenbrainz.py` + `api/listenbrainz_views.py`), not additions to the large existing `api/views.py`.

---

## Payload contract (all tasks below implement pieces of this)

```json
{
  "event_type": "play | pause | stop | mark_played | mark_unplayed",
  "media_type": "movie | tv",
  "tmdb_id": "603",
  "imdb_id": null,
  "tvdb_id": null,
  "season": null,
  "episode": null,
  "title": "The Matrix",
  "series_title": null,
  "position_seconds": 0,
  "duration_seconds": null,
  "played_to_completion": null
}
```

- `event_type` and `media_type` are required; everything else is optional per event.
- `season`/`episode` required when `media_type` is `"tv"`.
- `played_to_completion` is only meaningful on `event_type: "stop"` (maps 1:1 to Jellyfin's own `PlaybackStopEventArgs.PlayedToCompletion`, which the future plugin will read directly — no more inferring completion from a `Played` flag mid-session).

---

### Task 1: Endpoint scaffold — auth, validation, URL wiring

**Files:**
- Create: `src/integrations/webhooks/jellyfin_plugin.py`
- Create: `src/api/jellyfin_plugin_views.py`
- Modify: `src/api/urls.py`
- Test: `src/api/tests/test_jellyfin_plugin_views.py`

**Interfaces:**
- Produces: `JellyfinPluginEventProcessor` class in `integrations/webhooks/jellyfin_plugin.py` with method `process_event(self, payload: dict, user) -> None`, and module-level `SUPPORTED_EVENT_TYPES = {"play", "pause", "stop", "mark_played", "mark_unplayed"}`.
- Produces: `JellyfinPluginEventView` DRF `APIView` in `api/jellyfin_plugin_views.py`, mounted at `/api/v1/jellyfin-plugin/event/` under URL name `api_jellyfin_plugin_event`.
- Consumes: `api.authentication.APIKeyAuthentication` (existing).

- [ ] **Step 1: Write the failing test**

```python
# src/api/tests/test_jellyfin_plugin_views.py
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

EVENT_URL = "/api/v1/jellyfin-plugin/event/"


class JellyfinPluginEventViewTests(APITestCase):
    """Tests for the Floppy Jellyfin plugin ingest endpoint."""

    def setUp(self):
        """Create a user with a known API token."""
        self.user = get_user_model().objects.create_user(username="plugin-user")
        self.auth = {"HTTP_X_API_KEY": self.user.token}

    def test_missing_api_key_returns_401(self):
        """Test the endpoint rejects requests without an API key."""
        response = self.client.post(EVENT_URL, {}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_invalid_api_key_returns_401(self):
        """Test the endpoint rejects an unknown API key."""
        response = self.client.post(
            EVENT_URL,
            {},
            format="json",
            HTTP_X_API_KEY="not-a-real-token",
        )
        self.assertEqual(response.status_code, 401)

    def test_missing_event_type_returns_400(self):
        """Test the endpoint rejects a payload with no event_type."""
        response = self.client.post(
            EVENT_URL,
            {"media_type": "movie"},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 400)

    def test_unsupported_event_type_returns_400(self):
        """Test the endpoint rejects an unknown event_type."""
        response = self.client.post(
            EVENT_URL,
            {"event_type": "rewind", "media_type": "movie"},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 400)

    def test_missing_media_type_returns_400(self):
        """Test the endpoint rejects a payload with no media_type."""
        response = self.client.post(
            EVENT_URL,
            {"event_type": "play"},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 400)

    def test_valid_pause_event_returns_200(self):
        """Test a minimal, valid event is accepted.

        Pause events skip tracking entirely (mirrors the existing Jellyfin
        webhook), so this exercises the plumbing without needing any TMDB
        mocking.
        """
        response = self.client.post(
            EVENT_URL,
            {"event_type": "pause", "media_type": "movie", "tmdb_id": "603"},
            format="json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd src && python manage.py test api.tests.test_jellyfin_plugin_views -v 2`
Expected: FAIL — `404` on all requests (URL doesn't exist yet), or import error.

- [ ] **Step 3: Write the processor skeleton**

```python
# src/integrations/webhooks/jellyfin_plugin.py
"""Processor for events sent by the native Floppy Jellyfin plugin.

Unlike integrations.webhooks.jellyfin.JellyfinWebhookProcessor, this
processor is fed a clean, purpose-built payload (see the plugin's own
docs for the schema) rather than parsed Jellyfin webhook JSON — the
plugin has direct access to Jellyfin's internal session/item objects,
so no ticks-parsing or webhook-template quirks are needed here.
"""

import logging

from app.models import MediaTypes

from .base import BaseWebhookProcessor

logger = logging.getLogger(__name__)

SUPPORTED_EVENT_TYPES = {"play", "pause", "stop", "mark_played", "mark_unplayed"}


class JellyfinPluginEventProcessor(BaseWebhookProcessor):
    """Processor for Floppy Jellyfin plugin events."""

    def process_event(self, payload, user):
        """Process one event from the Floppy Jellyfin plugin."""
        event_type = payload.get("event_type")

        if event_type == "pause":
            return

        # Remaining event types are wired up in later tasks.

    def _is_supported_event(self, event_type, user=None):
        return event_type in SUPPORTED_EVENT_TYPES
```

- [ ] **Step 4: Write the view**

```python
# src/api/jellyfin_plugin_views.py
"""Ingest endpoint for the native Floppy Jellyfin plugin.

Receive-only: accepts structured playback/watch-state events from the
plugin and hands them to JellyfinPluginEventProcessor. See
integrations/webhooks/jellyfin_plugin.py for the payload contract.
"""

import logging
from http import HTTPStatus as HTTP  # noqa: N814

from drf_spectacular.utils import extend_schema
from rest_framework import views as drf_views
from rest_framework.response import Response

from api.authentication import APIKeyAuthentication
from integrations.webhooks.jellyfin_plugin import (
    SUPPORTED_EVENT_TYPES,
    JellyfinPluginEventProcessor,
)

logger = logging.getLogger(__name__)

_MEDIA_TYPES = ("movie", "tv")


def _error(message):
    """Return an error response body."""
    return Response({"error": message}, status=HTTP.BAD_REQUEST)


@extend_schema(
    summary="Submit a playback/watch-state event (Floppy Jellyfin plugin)",
    description=(
        "Accepts structured playback events from the native Floppy Jellyfin "
        "plugin. Authenticate with the `X-API-Key` header, using the same "
        "token shown on the Integrations page."
    ),
    request={
        "application/json": {
            "type": "object",
            "properties": {
                "event_type": {
                    "type": "string",
                    "enum": sorted(SUPPORTED_EVENT_TYPES),
                },
                "media_type": {"type": "string", "enum": list(_MEDIA_TYPES)},
                "tmdb_id": {"type": "string"},
                "imdb_id": {"type": "string"},
                "tvdb_id": {"type": "string"},
                "season": {"type": "integer"},
                "episode": {"type": "integer"},
                "title": {"type": "string"},
                "series_title": {"type": "string"},
                "position_seconds": {"type": "integer"},
                "duration_seconds": {"type": "integer"},
                "played_to_completion": {"type": "boolean"},
            },
            "required": ["event_type", "media_type"],
        },
    },
    responses={
        200: {"type": "object", "properties": {"status": {"type": "string"}}},
        400: {"type": "object", "properties": {"error": {"type": "string"}}},
    },
)
class JellyfinPluginEventView(drf_views.APIView):
    """Ingest endpoint for the Floppy Jellyfin plugin."""

    authentication_classes = [APIKeyAuthentication]

    def post(self, request):
        """Validate and process one plugin event."""
        payload = request.data
        if not isinstance(payload, dict):
            return _error("JSON document must be a single object.")

        event_type = payload.get("event_type")
        if event_type not in SUPPORTED_EVENT_TYPES:
            return _error(
                f"Invalid event_type. Must be one of: "
                f"{', '.join(sorted(SUPPORTED_EVENT_TYPES))}.",
            )

        media_type = payload.get("media_type")
        if media_type not in _MEDIA_TYPES:
            return _error(
                f"Invalid media_type. Must be one of: {', '.join(_MEDIA_TYPES)}.",
            )

        JellyfinPluginEventProcessor().process_event(payload, request.user)

        return Response({"status": "ok"}, status=HTTP.OK)
```

- [ ] **Step 5: Wire the URL**

Read `src/api/urls.py` first to see the existing `re_path` entries, then add (near the top, alongside other single-purpose views):

```python
# In src/api/urls.py, add to the import line and urlpatterns list:
from . import jellyfin_plugin_views, views

urlpatterns = [
    re_path(
        r"^jellyfin-plugin/event/?$",
        jellyfin_plugin_views.JellyfinPluginEventView.as_view(),
        name="api_jellyfin_plugin_event",
    ),
    # ... existing entries below, unchanged
]
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd src && python manage.py test api.tests.test_jellyfin_plugin_views -v 2`
Expected: PASS (all 6 tests)

- [ ] **Step 7: Commit**

```bash
git add src/integrations/webhooks/jellyfin_plugin.py src/api/jellyfin_plugin_views.py src/api/urls.py src/api/tests/test_jellyfin_plugin_views.py
git commit -m "feat: scaffold Jellyfin plugin ingest endpoint"
```

---

### Task 2: Movie tracking (play / stop)

**Files:**
- Modify: `src/integrations/webhooks/jellyfin_plugin.py`
- Test: `src/api/tests/test_jellyfin_plugin_views.py`

**Interfaces:**
- Consumes: `BaseWebhookProcessor._process_media(self, payload, user, ids)` (existing, `base.py:84`), which dispatches to `_process_movie`/`_process_tv` based on `self._get_media_type(payload)`.
- Produces: `JellyfinPluginEventProcessor` now implements `_get_media_type`, `_get_media_title`, `_extract_external_ids`, `_is_played`, `_is_unplayed`, `_extract_season_episode_from_payload`, `_extract_series_title` — the full abstract-method contract `BaseWebhookProcessor` expects.

- [ ] **Step 1: Write the failing tests**

Add to `src/api/tests/test_jellyfin_plugin_views.py`:

```python
from unittest.mock import patch

from app.models import Movie, Status


class JellyfinPluginMovieTests(APITestCase):
    """Tests for movie tracking via the plugin endpoint."""

    def setUp(self):
        """Create a user with a known API token."""
        self.user = get_user_model().objects.create_user(username="plugin-movie-user")
        self.auth = {"HTTP_X_API_KEY": self.user.token}

    @patch("app.providers.tmdb.movie")
    def test_play_creates_in_progress_movie(self, mock_movie):
        """Test a play event creates an in-progress Movie instance."""
        mock_movie.return_value = {"title": "The Matrix", "image": "matrix.jpg"}

        response = self.client.post(
            EVENT_URL,
            {"event_type": "play", "media_type": "movie", "tmdb_id": "603"},
            format="json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 200)
        movie = Movie.objects.get(item__media_id="603", user=self.user)
        self.assertEqual(movie.status, Status.IN_PROGRESS.value)
        self.assertEqual(movie.progress, 0)

    @patch("app.providers.tmdb.movie")
    def test_stop_with_played_to_completion_completes_movie(self, mock_movie):
        """Test a stop event with played_to_completion=True completes the movie."""
        mock_movie.return_value = {"title": "The Matrix", "image": "matrix.jpg"}

        self.client.post(
            EVENT_URL,
            {"event_type": "play", "media_type": "movie", "tmdb_id": "603"},
            format="json",
            **self.auth,
        )
        response = self.client.post(
            EVENT_URL,
            {
                "event_type": "stop",
                "media_type": "movie",
                "tmdb_id": "603",
                "played_to_completion": True,
            },
            format="json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 200)
        movie = Movie.objects.get(item__media_id="603", user=self.user)
        self.assertEqual(movie.status, Status.COMPLETED.value)
        self.assertEqual(movie.progress, 1)

    @patch("app.providers.tmdb.movie")
    def test_stop_without_played_to_completion_stays_in_progress(self, mock_movie):
        """Test stopping partway through leaves the movie in progress."""
        mock_movie.return_value = {"title": "The Matrix", "image": "matrix.jpg"}

        self.client.post(
            EVENT_URL,
            {"event_type": "play", "media_type": "movie", "tmdb_id": "603"},
            format="json",
            **self.auth,
        )
        response = self.client.post(
            EVENT_URL,
            {
                "event_type": "stop",
                "media_type": "movie",
                "tmdb_id": "603",
                "played_to_completion": False,
            },
            format="json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 200)
        movie = Movie.objects.get(item__media_id="603", user=self.user)
        self.assertEqual(movie.status, Status.IN_PROGRESS.value)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd src && python manage.py test api.tests.test_jellyfin_plugin_views.JellyfinPluginMovieTests -v 2`
Expected: FAIL — `NotImplementedError` from unimplemented abstract methods, or `KeyError`/`AttributeError`.

- [ ] **Step 3: Implement the processor's movie path**

Replace the contents of `src/integrations/webhooks/jellyfin_plugin.py` with:

```python
"""Processor for events sent by the native Floppy Jellyfin plugin.

Unlike integrations.webhooks.jellyfin.JellyfinWebhookProcessor, this
processor is fed a clean, purpose-built payload (see the plugin's own
docs for the schema) rather than parsed Jellyfin webhook JSON — the
plugin has direct access to Jellyfin's internal session/item objects,
so no ticks-parsing or webhook-template quirks are needed here.
"""

import logging

from app.models import MediaTypes

from .base import BaseWebhookProcessor

logger = logging.getLogger(__name__)

SUPPORTED_EVENT_TYPES = {"play", "pause", "stop", "mark_played", "mark_unplayed"}


class JellyfinPluginEventProcessor(BaseWebhookProcessor):
    """Processor for Floppy Jellyfin plugin events."""

    def process_event(self, payload, user):
        """Process one event from the Floppy Jellyfin plugin."""
        if payload.get("event_type") == "pause":
            return

        ids = self._extract_external_ids(payload)
        if not any(ids.values()):
            logger.warning("Ignoring Jellyfin plugin event with no external ID")
            return

        self._process_media(payload, user, ids)

    def _is_supported_event(self, event_type, user=None):
        return event_type in SUPPORTED_EVENT_TYPES

    def _is_played(self, payload):
        event_type = payload.get("event_type")
        if event_type == "mark_played":
            return True
        if event_type == "stop":
            return bool(payload.get("played_to_completion"))
        return False

    def _is_unplayed(self, payload):
        return payload.get("event_type") == "mark_unplayed"

    def _get_media_type(self, payload):
        return payload.get("media_type")

    def _get_media_title(self, payload):
        if self._get_media_type(payload) == MediaTypes.TV.value:
            series_title = payload.get("series_title")
            season = payload.get("season")
            episode = payload.get("episode")
            if series_title and season is not None and episode is not None:
                return f"{series_title} S{season:02d}E{episode:02d}"
            return series_title
        return payload.get("title")

    def _extract_external_ids(self, payload):
        return {
            "tmdb_id": payload.get("tmdb_id"),
            "imdb_id": payload.get("imdb_id"),
            "tvdb_id": payload.get("tvdb_id"),
        }

    def _extract_season_episode_from_payload(self, payload):
        season = payload.get("season")
        episode = payload.get("episode")
        try:
            season = int(season) if season is not None else None
            episode = int(episode) if episode is not None else None
        except (TypeError, ValueError):
            return None, None
        return season, episode

    def _extract_series_title(self, payload):
        if self._get_media_type(payload) == MediaTypes.TV.value:
            return payload.get("series_title")
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd src && python manage.py test api.tests.test_jellyfin_plugin_views -v 2`
Expected: PASS (all tests from Task 1 and Task 2)

- [ ] **Step 5: Commit**

```bash
git add src/integrations/webhooks/jellyfin_plugin.py src/api/tests/test_jellyfin_plugin_views.py
git commit -m "feat: track movie play/stop events from Jellyfin plugin"
```

---

### Task 3: TV episode tracking

**Files:**
- Test: `src/api/tests/test_jellyfin_plugin_views.py`

No processor changes needed — `_process_media` (reused in Task 2) already dispatches TV payloads to `BaseWebhookProcessor._process_tv`, which uses the `_extract_season_episode_from_payload`/`_extract_external_ids`/`_extract_series_title` overrides from Task 3. This task is test-only, confirming that reuse actually works end-to-end.

**Interfaces:**
- Consumes: `BaseWebhookProcessor._process_tv` (existing, `base.py:98`), `_handle_tv_episode` (existing, `base.py:1185`).

- [ ] **Step 1: Write the failing test**

Add to `src/api/tests/test_jellyfin_plugin_views.py`:

```python
from app.models import TV, Episode, Season


class JellyfinPluginTvTests(APITestCase):
    """Tests for TV episode tracking via the plugin endpoint."""

    def setUp(self):
        """Create a user with a known API token."""
        self.user = get_user_model().objects.create_user(username="plugin-tv-user")
        self.auth = {"HTTP_X_API_KEY": self.user.token}

    @patch("app.providers.tmdb.tv_with_seasons")
    def test_stop_with_played_to_completion_completes_episode(self, mock_tv):
        """Test a completed episode stop event creates a completed Episode."""
        mock_tv.return_value = {
            "media_id": "1418",
            "title": "The Big Bang Theory",
            "image": "tbbt.jpg",
            "season/1": {
                "episodes": [{"episode_number": 1, "title": "Pilot"}],
            },
        }

        response = self.client.post(
            EVENT_URL,
            {
                "event_type": "stop",
                "media_type": "tv",
                "tmdb_id": "1418",
                "season": 1,
                "episode": 1,
                "series_title": "The Big Bang Theory",
                "played_to_completion": True,
            },
            format="json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 200)
        episode = Episode.objects.get(
            item__media_id="1418",
            item__season_number=1,
            item__episode_number=1,
        )
        self.assertEqual(episode.status, Status.COMPLETED.value)
```

This query pattern (`item__media_id`/`item__season_number`/`item__episode_number`, no `user` filter on `Episode` itself) is copied directly from the existing working test at `integrations/tests/test_webhooks_jellyfin.py:84-88` — each episode has its own `Item` row carrying `season_number`/`episode_number`, distinct from the show's own `Item`.

- [ ] **Step 2: Run test to verify it passes**

Run: `cd src && python manage.py test api.tests.test_jellyfin_plugin_views.JellyfinPluginTvTests -v 2`
Expected: PASS — `_process_media` (reused in Task 2) already routes `media_type: "tv"` payloads to `_process_tv`, so no new implementation should be needed. If it fails, that's a real bug to fix in `jellyfin_plugin.py`, not a test issue.

- [ ] **Step 3: Run full test suite for this file**

Run: `cd src && python manage.py test api.tests.test_jellyfin_plugin_views -v 2`
Expected: PASS (all tests from Tasks 1–3)

- [ ] **Step 4: Commit**

```bash
git add src/api/tests/test_jellyfin_plugin_views.py
git commit -m "test: confirm TV episode tracking via Jellyfin plugin endpoint"
```

---

### Task 4: MarkPlayed / MarkUnplayed

**Files:**
- Test: `src/api/tests/test_jellyfin_plugin_views.py`

No processor changes needed — `_is_played`/`_is_unplayed` (Task 2) already branch on `event_type in ("mark_played", "mark_unplayed")`, and `_handle_movie`/`_handle_tv_episode` already call those methods. This task is test-only, same rationale as Task 3.

- [ ] **Step 1: Write the failing tests**

Add to `JellyfinPluginMovieTests` in `src/api/tests/test_jellyfin_plugin_views.py`:

```python
    @patch("app.providers.tmdb.movie")
    def test_mark_played_completes_movie_without_a_play_event(self, mock_movie):
        """Test mark_played completes a movie with no prior play event."""
        mock_movie.return_value = {"title": "The Matrix", "image": "matrix.jpg"}

        response = self.client.post(
            EVENT_URL,
            {"event_type": "mark_played", "media_type": "movie", "tmdb_id": "603"},
            format="json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 200)
        movie = Movie.objects.get(item__media_id="603", user=self.user)
        self.assertEqual(movie.status, Status.COMPLETED.value)

    @patch("app.providers.tmdb.movie")
    def test_mark_unplayed_deletes_movie(self, mock_movie):
        """Test mark_unplayed removes an existing Movie instance."""
        mock_movie.return_value = {"title": "The Matrix", "image": "matrix.jpg"}

        self.client.post(
            EVENT_URL,
            {"event_type": "mark_played", "media_type": "movie", "tmdb_id": "603"},
            format="json",
            **self.auth,
        )
        response = self.client.post(
            EVENT_URL,
            {"event_type": "mark_unplayed", "media_type": "movie", "tmdb_id": "603"},
            format="json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            Movie.objects.filter(item__media_id="603", user=self.user).exists(),
        )
```

- [ ] **Step 2: Run tests to verify current behavior**

Run: `cd src && python manage.py test api.tests.test_jellyfin_plugin_views.JellyfinPluginMovieTests -v 2`
Expected: PASS immediately, since Task 2 already implemented `_is_played`/`_is_unplayed` to branch on these event types. If either test fails, fix `_is_played`/`_is_unplayed` in `jellyfin_plugin.py` until both pass — do not weaken the test.

- [ ] **Step 3: Commit**

```bash
git add src/api/tests/test_jellyfin_plugin_views.py
git commit -m "test: confirm mark_played/mark_unplayed via Jellyfin plugin endpoint"
```

---

### Task 5: Live playback card wiring

**Files:**
- Modify: `src/integrations/webhooks/jellyfin_plugin.py`
- Test: `src/api/tests/test_jellyfin_plugin_views.py`

**Interfaces:**
- Consumes: `app.live_playback.apply_playback_event(*, user_id, event_type, playback_media_type, media_id=None, source=..., rating_key=None, title=None, series_title=None, episode_title=None, season_number=None, episode_number=None, view_offset_seconds=None, duration_seconds=None) -> None` (existing, `app/live_playback.py:127`).
- Consumes (TV only): `BaseWebhookProcessor._find_tv_media_id(self, ids, series_title=None, allow_title_fallback=False, year=None)` (existing, `base.py:681`) — resolves a TV episode's show-level TMDB id the same way the tracking path does, so the live-playback card and the tracked history agree.

This design note is unique to the plugin processor: the existing `JellyfinWebhookProcessor._update_live_playback_state` (in `jellyfin.py`) has a longer TVDB/IMDB-preferring fallback chain because it's working from raw, sometimes-incomplete Jellyfin webhook JSON. The plugin processor's payload always carries `season`/`episode` directly (no extraction needed) and a `tmdb_id` when the plugin already resolved one, so its version can be simpler: try `_find_tv_media_id` once, and fall back to the raw `tmdb_id` if that returns nothing.

- [ ] **Step 1: Write the failing tests**

Add to `src/api/tests/test_jellyfin_plugin_views.py`:

```python
from app import live_playback


class JellyfinPluginLivePlaybackTests(APITestCase):
    """Tests for live-playback card updates via the plugin endpoint."""

    def setUp(self):
        """Create a user with a known API token."""
        self.user = get_user_model().objects.create_user(username="plugin-live-user")
        self.auth = {"HTTP_X_API_KEY": self.user.token}

    def tearDown(self):
        """Clear cached playback state created by these tests."""
        live_playback.clear_user_playback_state(self.user.id)

    @patch("app.providers.tmdb.movie")
    def test_play_updates_live_playback_card(self, mock_movie):
        """Test a play event updates the cached live-playback state."""
        mock_movie.return_value = {"title": "The Matrix", "image": "matrix.jpg"}

        response = self.client.post(
            EVENT_URL,
            {
                "event_type": "play",
                "media_type": "movie",
                "tmdb_id": "603",
                "title": "The Matrix",
                "position_seconds": 30,
                "duration_seconds": 8160,
            },
            format="json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 200)
        state = live_playback.get_user_playback_state(self.user.id)
        self.assertIsNotNone(state)
        self.assertEqual(state["status"], live_playback.PLAYBACK_STATUS_PLAYING)
        self.assertEqual(state["media_id"], "603")

    @patch("app.providers.tmdb.movie")
    def test_pause_updates_live_playback_card(self, mock_movie):
        """Test a pause event updates the cached state without tracking."""
        mock_movie.return_value = {"title": "The Matrix", "image": "matrix.jpg"}

        self.client.post(
            EVENT_URL,
            {
                "event_type": "play",
                "media_type": "movie",
                "tmdb_id": "603",
                "position_seconds": 30,
                "duration_seconds": 8160,
            },
            format="json",
            **self.auth,
        )
        response = self.client.post(
            EVENT_URL,
            {
                "event_type": "pause",
                "media_type": "movie",
                "tmdb_id": "603",
                "position_seconds": 45,
                "duration_seconds": 8160,
            },
            format="json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 200)
        state = live_playback.get_user_playback_state(self.user.id)
        self.assertEqual(state["status"], live_playback.PLAYBACK_STATUS_PAUSED)

    @patch("app.providers.tmdb.movie")
    def test_stop_clears_live_playback_to_stopped(self, mock_movie):
        """Test a stop event marks the cached state as stopped."""
        mock_movie.return_value = {"title": "The Matrix", "image": "matrix.jpg"}

        self.client.post(
            EVENT_URL,
            {"event_type": "play", "media_type": "movie", "tmdb_id": "603"},
            format="json",
            **self.auth,
        )
        response = self.client.post(
            EVENT_URL,
            {
                "event_type": "stop",
                "media_type": "movie",
                "tmdb_id": "603",
                "played_to_completion": True,
            },
            format="json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 200)
        state = live_playback.get_user_playback_state(self.user.id)
        self.assertEqual(state["status"], live_playback.PLAYBACK_STATUS_STOPPED)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd src && python manage.py test api.tests.test_jellyfin_plugin_views.JellyfinPluginLivePlaybackTests -v 2`
Expected: FAIL — `state` is `None` in all three (live playback is never updated yet).

- [ ] **Step 3: Implement live-playback wiring**

In `src/integrations/webhooks/jellyfin_plugin.py`, add the import and the new method, and call it from `process_event`:

```python
# Add to the imports at the top of the file:
from app import live_playback

# Add this constant near SUPPORTED_EVENT_TYPES:
_LIVE_PLAYBACK_EVENT_MAP = {
    "play": "media.play",
    "pause": "media.pause",
    "stop": "media.stop",
}
```

Replace `process_event` with:

```python
    def process_event(self, payload, user):
        """Process one event from the Floppy Jellyfin plugin."""
        ids = self._extract_external_ids(payload)

        self._update_live_playback_state(payload, user, ids)

        if payload.get("event_type") == "pause":
            return

        if not any(ids.values()):
            logger.warning("Ignoring Jellyfin plugin event with no external ID")
            return

        self._process_media(payload, user, ids)
```

Add the new method:

```python
    def _update_live_playback_state(self, payload, user, ids):
        """Update cache-backed live playback state for the home-page UI."""
        event_type = _LIVE_PLAYBACK_EVENT_MAP.get(payload.get("event_type"))
        if not event_type:
            return

        media_type = self._get_media_type(payload)
        if media_type not in (MediaTypes.MOVIE.value, MediaTypes.TV.value):
            return

        season_number, episode_number = self._extract_season_episode_from_payload(
            payload,
        )

        if media_type == MediaTypes.MOVIE.value:
            playback_media_type = MediaTypes.MOVIE.value
            media_id = ids.get("tmdb_id")
        else:
            playback_media_type = MediaTypes.EPISODE.value
            media_id, _, _ = self._find_tv_media_id(
                ids,
                series_title=self._extract_series_title(payload),
                allow_title_fallback=True,
            )
            if media_id is None:
                media_id = ids.get("tmdb_id")
            media_id = str(media_id) if media_id is not None else None

        live_playback.apply_playback_event(
            user_id=user.id,
            event_type=event_type,
            playback_media_type=playback_media_type,
            media_id=media_id,
            title=payload.get("title"),
            series_title=payload.get("series_title"),
            episode_title=(
                payload.get("title")
                if playback_media_type == MediaTypes.EPISODE.value
                else None
            ),
            season_number=season_number,
            episode_number=episode_number,
            view_offset_seconds=payload.get("position_seconds"),
            duration_seconds=payload.get("duration_seconds"),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd src && python manage.py test api.tests.test_jellyfin_plugin_views -v 2`
Expected: PASS (all tests from Tasks 1–5)

- [ ] **Step 5: Commit**

```bash
git add src/integrations/webhooks/jellyfin_plugin.py src/api/tests/test_jellyfin_plugin_views.py
git commit -m "feat: update live-playback card from Jellyfin plugin events"
```

---

## Self-review notes

- **Spec coverage:** Task 1 covers the API endpoint + auth from the design's "Floppy-side changes" section. Tasks 2–4 cover playback tracking and MarkPlayed/MarkUnplayed parity from the "Scope" section. Task 5 covers a gap found during planning that the approved design doc's architecture diagram didn't explicitly call out: the live-playback ("now watching") card. The design doc's own reasoning (citing `live_playback.py`'s interpolation model as evidence the new event volume is safe) implicitly assumed this wiring would exist — flagging this to the user rather than silently either adding or omitting it.
- **Type/query consistency:** all model field names and query paths (`Episode.objects.get(item__media_id=..., item__season_number=..., item__episode_number=...)`, `live_playback.apply_playback_event`'s keyword signature, `BaseWebhookProcessor`'s abstract methods) were checked directly against the current source and existing working tests rather than assumed — Task 3's query was corrected during self-review to match the exact pattern already proven in `test_webhooks_jellyfin.py:84-88`.
- **Not in this plan:** the C# Jellyfin plugin itself, and distribution/manifest hosting — tracked as a separate plan per the brainstorming scope-check, to be written next.
