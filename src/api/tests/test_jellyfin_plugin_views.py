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
