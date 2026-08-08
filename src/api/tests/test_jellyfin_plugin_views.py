from unittest.mock import patch

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from app.models import TV, Episode, Movie, Season, Status

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


class JellyfinPluginMovieTests(APITestCase):
    """Tests for movie tracking via the plugin endpoint."""

    def setUp(self):
        """Create a user with a known API token."""
        self.user = get_user_model().objects.create_user(username="plugin-movie-user")
        self.auth = {"HTTP_X_API_KEY": self.user.token}

    @patch("app.providers.tmdb.movie")
    def test_play_creates_in_progress_movie(self, mock_movie):
        """Test a play event creates an in-progress Movie instance."""
        mock_movie.return_value = {
            "title": "The Matrix",
            "image": "matrix.jpg",
            "max_progress": 1,
        }

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
        mock_movie.return_value = {
            "title": "The Matrix",
            "image": "matrix.jpg",
            "max_progress": 1,
        }

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
        mock_movie.return_value = {
            "title": "The Matrix",
            "image": "matrix.jpg",
            "max_progress": 1,
        }

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
