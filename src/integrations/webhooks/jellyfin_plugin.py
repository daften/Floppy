"""Processor for events sent by the native Floppy Jellyfin plugin.

Unlike integrations.webhooks.jellyfin.JellyfinWebhookProcessor, this
processor is fed a clean, purpose-built payload (see the plugin's own
docs for the schema) rather than parsed Jellyfin webhook JSON — the
plugin has direct access to Jellyfin's internal session/item objects,
so no ticks-parsing or webhook-template quirks are needed here.
"""

import logging

from app import live_playback
from app.models import MediaTypes

from .base import BaseWebhookProcessor

logger = logging.getLogger(__name__)

SUPPORTED_EVENT_TYPES = {"play", "pause", "stop", "mark_played", "mark_unplayed"}

_LIVE_PLAYBACK_EVENT_MAP = {
    "play": "media.play",
    "pause": "media.pause",
    "stop": "media.stop",
}


class JellyfinPluginEventProcessor(BaseWebhookProcessor):
    """Processor for Floppy Jellyfin plugin events."""

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
