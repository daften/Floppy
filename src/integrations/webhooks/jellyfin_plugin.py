"""Processor for events sent by the native Floppy Jellyfin plugin.

Unlike integrations.webhooks.jellyfin.JellyfinWebhookProcessor, this
processor is fed a clean, purpose-built payload (see the plugin's own
docs for the schema) rather than parsed Jellyfin webhook JSON — the
plugin has direct access to Jellyfin's internal session/item objects,
so no ticks-parsing or webhook-template quirks are needed here.
"""

import logging

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
