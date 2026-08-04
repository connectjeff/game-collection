from __future__ import annotations

import unittest
from unittest.mock import patch

from game_collection.providers import IgdbProvider


class IgdbProviderTests(unittest.TestCase):
    def test_search_uses_twitch_token_and_normalizes_cover_metadata(self) -> None:
        responses = [
            {"access_token": "token-123"},
            [
                {
                    "id": 123,
                    "name": "Metroid Prime",
                    "summary": "A first-person adventure.",
                    "first_release_date": 1036972800,
                    "cover": {"image_id": "co1234"},
                    "platforms": [{"name": "Nintendo GameCube"}],
                    "involved_companies": [
                        {"developer": True, "company": {"name": "Retro Studios"}},
                        {"publisher": True, "company": {"name": "Nintendo"}},
                    ],
                }
            ],
        ]

        with patch("game_collection.providers._post_json", side_effect=responses) as post_json:
            provider = IgdbProvider(client_id="client", client_secret="secret")
            matches = provider.search("Metroid Prime", platform="Nintendo GameCube")

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].provider, "igdb")
        self.assertEqual(matches[0].provider_game_id, "123")
        self.assertEqual(matches[0].title, "Metroid Prime")
        self.assertEqual(matches[0].release_date, "2002-11-11")
        self.assertEqual(matches[0].developer, "Retro Studios")
        self.assertEqual(matches[0].publisher, "Nintendo")
        self.assertEqual(
            matches[0].cover_url,
            "https://images.igdb.com/igdb/image/upload/t_cover_big/co1234.jpg",
        )
        self.assertEqual(post_json.call_count, 2)
        self.assertIn("oauth2/token", post_json.call_args_list[0].args[0])
        self.assertIn("/v4/games", post_json.call_args_list[1].args[0])

    def test_platforms_fetches_all_igdb_platforms(self) -> None:
        responses = [
            {"access_token": "token-123"},
            [{"id": 48, "name": "PlayStation 4"}, {"id": 167, "name": "PlayStation 5"}],
        ]

        with patch("game_collection.providers._post_json", side_effect=responses):
            provider = IgdbProvider(client_id="client", client_secret="secret")
            platforms = provider.platforms(limit=2)

        self.assertEqual(platforms, [{"id": 48, "name": "PlayStation 4"}, {"id": 167, "name": "PlayStation 5"}])


if __name__ == "__main__":
    unittest.main()
