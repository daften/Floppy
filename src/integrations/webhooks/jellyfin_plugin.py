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
