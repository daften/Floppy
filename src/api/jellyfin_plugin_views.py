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
