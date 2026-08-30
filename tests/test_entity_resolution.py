from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from angela_orchestration.entity_resolution import (
    LocationEntityResolver,
    SponsorEntityResolver,
    company_aliases,
    split_party_names,
)


class SponsorEntityResolverTests(unittest.TestCase):
    def test_site_locations_are_loaded_from_database_not_a_fixed_city_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trials.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(
                "CREATE TABLE trials(id INTEGER PRIMARY KEY,is_active INTEGER,"
                "current_version_id INTEGER,sponsor_name_raw TEXT);"
                "CREATE TABLE trial_sites(trial_version_id INTEGER,city TEXT,province TEXT);"
                "INSERT INTO trials VALUES(1,1,10,'示例公司');"
                "INSERT INTO trial_sites VALUES(10,'Alicante','Alicante');"
            )
            connection.commit()
            connection.close()
            matches = LocationEntityResolver(path).resolve("Alicante有研究中心的项目")
            self.assertEqual(matches[0].canonical_name, "Alicante")
            self.assertEqual(matches[0].kind, "city")

    def test_latin_locations_require_complete_token_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trials.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(
                "CREATE TABLE trials(id INTEGER PRIMARY KEY,is_active INTEGER,"
                "current_version_id INTEGER,sponsor_name_raw TEXT);"
                "CREATE TABLE trial_sites(trial_version_id INTEGER,city TEXT,province TEXT);"
                "INSERT INTO trials VALUES(1,1,10,'Example');"
                "INSERT INTO trial_sites VALUES(10,'OMA','Hemato');"
            )
            connection.commit()
            connection.close()
            resolver = LocationEntityResolver(path)
            self.assertEqual(resolver.resolve("lung adenocarcinoma trials"), ())
            self.assertEqual(resolver.resolve("systemic lupus erythematosus trials"), ())
            matches = resolver.resolve("show clinical trials in OMA.")
            self.assertEqual(matches[0].canonical_name, "OMA")

    def test_aliases_come_from_legal_name_morphology(self) -> None:
        aliases = company_aliases("星河医药（示例）股份有限公司")
        self.assertIn("星河医药（示例）股份有限公司", aliases)
        self.assertIn("星河医药", aliases)
        self.assertIn("星河", aliases)
        self.assertNotIn("医药", aliases)
        regional = company_aliases("远岚医药（示例）股份有限公司")
        self.assertIn("远岚医药", regional)
        self.assertNotIn("华东", regional)

    def test_database_entities_resolve_without_a_company_specific_map(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trials.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(
                "CREATE TABLE trials(id INTEGER PRIMARY KEY,is_active INTEGER,"
                "current_version_id INTEGER,sponsor_name_raw TEXT);"
                "CREATE TABLE trial_parties(trial_version_id INTEGER,role TEXT,name_raw TEXT);"
                "INSERT INTO trials VALUES(1,1,10,'星河医药（示例）股份有限公司');"
            )
            connection.commit()
            connection.close()
            resolver = SponsorEntityResolver(path)
            matches = resolver.resolve("星河有哪些G12D项目？")
            self.assertEqual(matches[0].raw_alias, "星河")
            self.assertEqual(
                matches[0].canonical_names,
                ("星河医药（示例）股份有限公司",),
            )

    def test_multi_party_source_field_resolves_each_legal_entity(self) -> None:
        self.assertIn("ExamplePharma", company_aliases("Example Pharma Ltd"))
        self.assertEqual(
            split_party_names(
                "Example Pharma Ltd/ 示例制药（虚构）有限公司/ 1）Sample Biotech, Inc."
            ),
            (
                "Example Pharma Ltd",
                "示例制药（虚构）有限公司",
                "Sample Biotech, Inc.",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trials.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(
                "CREATE TABLE trials(id INTEGER PRIMARY KEY,is_active INTEGER,"
                "current_version_id INTEGER,sponsor_name_raw TEXT);"
                "CREATE TABLE trial_parties(trial_version_id INTEGER,role TEXT,name_raw TEXT);"
                "INSERT INTO trials VALUES(1,1,10,'Example Pharma Ltd/ 示例制药（虚构）有限公司');"
            )
            connection.commit()
            connection.close()

            matches = SponsorEntityResolver(path).resolve(
                "Example Pharma目前有哪些KRAS G12C项目？"
            )
            self.assertEqual(matches[0].raw_alias, "examplepharma")


if __name__ == "__main__":
    unittest.main()
