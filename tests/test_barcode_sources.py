from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from game_collection.barcode_match import read_barcode_catalog
from game_collection.barcode_sources import (
    download_csv_url,
    download_open_products_facts_products,
    download_upcdev_products,
    download_upcdev_search,
    download_wikidata_video_game_barcodes,
    lookup_live_barcode,
)


class BarcodeSourceTests(unittest.TestCase):
    def test_downloads_wikidata_video_game_barcodes(self) -> None:
        payload = {
            "results": {
                "bindings": [
                    {
                        "item": {"value": "https://www.wikidata.org/entity/Q1"},
                        "itemLabel": {"value": "Example Game"},
                        "gtin": {"value": "045496905651"},
                        "platformLabel": {"value": "Nintendo Switch"},
                    }
                ]
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "wikidata.csv"
            with patch("game_collection.barcode_sources._get_json", return_value=payload):
                entries = download_wikidata_video_game_barcodes(out, limit=1)
            rows = read_barcode_catalog(out)

        self.assertEqual(len(entries), 1)
        self.assertEqual(rows[0].provider, "wikidata")
        self.assertEqual(rows[0].provider_game_id, "Q1")

    def test_downloads_upcdev_search_results(self) -> None:
        payload = {
            "data": {
                "products": [
                    {
                        "upc": "045496905651",
                        "name": "Super Mario Galaxy + Super Mario Galaxy 2 Nintendo Switch",
                        "brand": "Nintendo",
                        "category": "Video Games",
                    }
                ]
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "upcdev.csv"
            with patch("game_collection.barcode_sources._get_json", return_value=payload):
                entries = download_upcdev_search(out, query="Super Mario Galaxy")

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].provider, "upcdev")
        self.assertEqual(entries[0].platform, "Nintendo Switch")

    def test_downloads_upcdev_product_results(self) -> None:
        payload = {
            "data": {
                "upc": "889842123456",
                "name": "Example Xbox Series X Game",
                "brand": "Microsoft",
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "upcdev-product.csv"
            with patch("game_collection.barcode_sources._get_json", return_value=payload):
                entries = download_upcdev_products(out, barcodes=["889842123456"])

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].platform, "Xbox Series X|S")

    def test_downloads_open_products_facts_results(self) -> None:
        payload = {
            "product": {
                "product_name": "Example PlayStation 5 Game",
                "brands": "Sony",
                "categories": "Video Games, PlayStation 5",
                "image_url": "https://example.test/image.jpg",
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "opf.csv"
            with patch("game_collection.barcode_sources._get_json", return_value=payload):
                entries = download_open_products_facts_products(out, barcodes=["711719541028"])

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].provider, "openproductsfacts")
        self.assertEqual(entries[0].platform, "PlayStation 5")

    def test_live_lookup_uses_upcdev_product_after_pricecharting_miss(self) -> None:
        payload = {
            "data": {
                "upc": "045496905651",
                "name": "Super Mario Galaxy + Super Mario Galaxy 2 Nintendo Switch",
                "brand": "Nintendo",
            }
        }
        with (
            patch("game_collection.barcode_sources._lookup_pricecharting_redirect", return_value=None),
            patch("game_collection.barcode_sources._get_json", return_value=payload),
        ):
            entry = lookup_live_barcode("045496905651", platform="Nintendo Switch")

        self.assertIsNotNone(entry)
        self.assertEqual(entry.title, "Super Mario Galaxy + Super Mario Galaxy 2 Nintendo Switch")
        self.assertEqual(entry.provider, "upcdev")
        self.assertEqual(entry.platform, "Nintendo Switch")

    def test_download_csv_url_incrementally_merges_existing_rows(self) -> None:
        csv_text = (
            "upc,product-name,console-name\n"
            "889842123456,Example Xbox Series X Game,Xbox Series X|S\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "source.csv"
            out.write_text(
                "barcode,title,platform,provider,provider_game_id,release_date,developer,publisher,description,cover_url\n"
                "045496905651,Super Mario Galaxy + Super Mario Galaxy 2,Nintendo Switch,wikidata,Q1,,,,,\n",
                encoding="utf-8",
            )
            with patch("game_collection.barcode_sources._read_url_text", return_value=csv_text):
                entries = download_csv_url(out, url="https://example.test/source.csv", incremental=True)

        self.assertEqual(len(entries), 2)
        self.assertEqual({entry.platform for entry in entries}, {"Nintendo Switch", "Xbox Series X|S"})


if __name__ == "__main__":
    unittest.main()
