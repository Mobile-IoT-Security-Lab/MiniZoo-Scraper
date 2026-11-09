import csv
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import wechat_csv_scraper as scraper


class WechatCsvMetadataTests(unittest.TestCase):
    def test_input_metadata_is_preserved_only_as_search_context(self):
        fields = scraper.manifest_fields()
        row = {
            "appid": "wxabc",
            "doc_id": "123",
            "name": "Demo Mini App",
            "description": "Demo description",
            "icon_url": "https://example.invalid/icon.png",
            "username": "gh_demo@app",
            "developer": "Demo Developer Ltd",
            "app_version": "42",
            "source_query": "demo category",
            "verified_at": "2026-09-04T11:29:20+00:00",
        }

        rec = scraper.make_manifest_record(row, fields, id_column="appid", name_column="name")

        import json
        self.assertEqual(rec["queried_csv_id"], "wxabc")
        self.assertEqual(rec["queried_appid"], "wxabc")
        self.assertEqual(rec["queried_developer"], "Demo Developer Ltd")
        context = json.loads(rec["query_metadata_json"])
        self.assertEqual(context["username"], "gh_demo@app")
        self.assertEqual(context["app_version"], "42")
        self.assertNotIn("category", context)
        self.assertEqual(context["source_query"], "demo category")
        self.assertEqual(context["description"], "Demo description")
        for key in ("appid", "name", "developer", "username", "app_version", "category", "description", "icon_url", "metadata_verified_at"):
            self.assertEqual(rec[key], "")

    def test_explicit_category_is_preserved_in_search_context(self):
        import json
        row = {"appid": "wx0123456789abcdef", "name": "Example",
               "category": "Travel", "source_query": "tickets"}
        rec = scraper.make_manifest_record(row, scraper.manifest_fields())
        context = json.loads(rec["query_metadata_json"])
        self.assertEqual(context["category"], "Travel")
        self.assertEqual(context["source_query"], "tickets")

    def test_append_package_history_writes_one_row_per_package(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            package = tmp_path / "demo.wxapkg"
            package.write_bytes(b"wx package bytes")
            digest = scraper.sha256(package)
            out = tmp_path / "run"
            out.mkdir()
            rel = Path("packages_by_miniapp/001_demo/demo.wxapkg")
            local_copy = out / rel
            local_copy.parent.mkdir(parents=True)
            local_copy.write_bytes(package.read_bytes())

            rec = {field: "" for field in scraper.manifest_fields()}
            rec.update(
                {
                    "csv_id": "wxabc",
                    "appid": "wxabc",
                    "name": "Demo Mini App",
                    "category": "tools",
                    "developer": "Demo Developer Ltd",
                    "username": "gh_demo@app",
                    "app_version": "42",
                    "description": "Demo description",
                    "icon_url": "https://example.invalid/icon.png",
                    "status": "ok",
                    "selection_mode": "new_since_open",
                    "local_files": str(rel),
                    "sha256": digest,
                    "remote_sources": "/data/data/com.tencent.mm/MicroMsg/appbrand/pkg/general/demo_42.wxapkg",
                }
            )

            history = out / "package_history.csv"
            scraper.append_package_history(
                history,
                rec,
                out,
                source_run="test_run",
                collected_at="2026-09-07T10:00:00+00:00",
            )

            with history.open(encoding="utf-8") as source:
                rows = list(csv.DictReader(source))
            self.assertEqual(len(rows), 1)
            row = rows[0]
            # Downloaded bytes are unassigned; the query is not host identity.
            self.assertEqual(row["appid"], "")
            self.assertEqual(row["package_sha256"], digest)
            self.assertEqual(row["manifest_sha256"], digest)
            self.assertEqual(row["hash_status"], "match")
            self.assertEqual(row["package_size_bytes"], str(len(b"wx package bytes")))
            self.assertEqual(row["developer"], "")
            self.assertEqual(row["app_version"], "")
            self.assertEqual(row["selection_mode"], "context_candidate_unassigned")
            self.assertEqual(row["source_run"], "test_run")
            with (out / "package_metadata.csv").open(encoding="utf-8") as source:
                metadata = list(csv.DictReader(source))
            self.assertEqual(len(metadata), 1)
            self.assertEqual(metadata[0]["queried_appid"], "wxabc")
            self.assertEqual(metadata[0]["queried_app_version"], "42")
            self.assertEqual(metadata[0]["package_attribution"], "UNASSIGNED")


if __name__ == "__main__":
    unittest.main()
