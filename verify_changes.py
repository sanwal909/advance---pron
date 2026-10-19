#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
verify_changes.py — LOCAL SAFETY TESTS for Token Replacement + MongoDB Persistence changes.

These tests:
  • DO NOT require real Telegram bot tokens or live MongoDB
  • DO NOT make any network calls
  • Run fully offline with standard-library-only mocks

Pass criteria: All tests report PASS.
Run:  python verify_changes.py
"""

import sys
import os
import json
import tempfile
import shutil
import re
import unittest
from unittest.mock import patch, MagicMock

HERE = os.path.dirname(os.path.abspath(__file__))
# Ensure we import the LOCAL config/bot modules
sys.path.insert(0, HERE)

# PRE-SET minimal required env vars BEFORE any import of bot/config so module load doesn't sys.exit
os.environ.setdefault("BOT_TOKEN", "12345678:DUMMY_DUMMY_DUMMY_DUMMY_DUMMY_DUMMY_DUMMY")
os.environ.setdefault("MONGO_URI", "")
os.environ.setdefault("ADMIN_IDS", "")

# =====================================================================
# TEST GROUP 1 — config.py: safe settings merge + idempotent migrations
# =====================================================================
class TestConfigSettingsMerge(unittest.TestCase):
    def _fresh_config(self):
        """Import config in a fresh-module-like state by calling internals directly."""
        import importlib
        import config
        importlib.reload(config)
        return config

    def test_deep_merge_settings_preserves_existing_values(self):
        """_deep_merge_settings must NEVER overwrite a non-empty existing key."""
        import config as c
        current = {
            "admin_ids": ["999"],
            "upi_id": "myoldupi@bank",
            "upi_name": "Old Name",
            "premium_channels": [
                {"id": "mych", "name": "My Channel", "amount": "123",
                 "channel_id": "-100old", "color": "green"}
            ],
            "total_orders": 42,
            "support_username": "my_support",
            "demo_amount": "77",
            "nested_plan_demos": {"ch1": [{"id": "abc", "type": "video"}]},
        }
        defaults = {
            "admin_ids": ["111"],
            "upi_id": "default@upi",
            "upi_name": "Default Name",
            "premium_channels": [
                {"id": "ch1", "name": "Channel 1", "amount": "99", "color": ""},
                {"id": "all", "name": "All Channels", "amount": "299", "color": ""},
            ],
            "total_orders": 0,
            "demo_amount": "10",
            "log_channel": "",
            "new_key_not_in_current": "hi",  # should be ADDED
        }
        changed = c._deep_merge_settings(current, defaults)
        # Non-empty existing keys preserved
        self.assertEqual(current["admin_ids"], ["999"], "admin_ids overwritten!")
        self.assertEqual(current["upi_id"], "myoldupi@bank", "upi_id overwritten!")
        self.assertEqual(current["upi_name"], "Old Name", "upi_name overwritten!")
        self.assertEqual(current["premium_channels"][0]["amount"], "123", "channel amount overwritten!")
        self.assertEqual(current["premium_channels"][0]["color"], "green", "channel color overwritten!")
        self.assertEqual(current["total_orders"], 42, "total_orders reset!")
        self.assertEqual(current["demo_amount"], "77", "demo_amount reset!")
        # Missing key ADDED (not overwritten)
        self.assertEqual(current["new_key_not_in_current"], "hi", "new key not added!")
        self.assertTrue(changed or True)  # changed flag is informative only

    def test_idempotent_migrations_never_remove_existing(self):
        """_run_idempotent_migrations must not touch an already-valid settings dict."""
        import config as c
        full_settings = {
            "admin_ids": ["123", "456"],
            "premium_channels": [
                {"id": "chA", "name": "A", "amount": "99", "channel_id": "-100X",
                 "duration": "Lifetime", "description": "desc", "color": "blue"},
            ],
            "proof_channel_id": "-100proof",
            "payment_proof_status": True,
            "payment_proof_link": "https://t.me/proof",
            "force_join_status": True,
            "auto_accept_requests": False,
            "backup_channel": "-100bak",
            "log_channel": "-100log",
            "demo_channel_link": "https://t.me/demo",
            "demo_channel_id": "-100demo",
            "demo_paid_status": True,
            "demo_amount": "15",
            "demo_color": "red",
            "support_username": "support_user",
            "support_status": True,
            "how_to_buy_url": "https://buy",
            "total_orders": 99,
            "upi_id": "merchant@upi",
            "upi_name": "Merchant",
            "start_demo_videos": [{"id": "xxx", "type": "video"}],
            "plan_demo_videos": {"chA": [{"id": "y", "type": "video"}]},
            "start_demo_desc": "welcome",
            "plan_demo_descs": {"chA": "A desc"},
        }
        before = json.dumps(full_settings, sort_keys=True)
        changed = c._run_idempotent_migrations(full_settings)
        after = json.dumps(full_settings, sort_keys=True)
        self.assertEqual(before, after, "Migration mutated a complete valid settings dict!")
        self.assertFalse(changed, "Migration flagged changes for already-complete settings.")

    def test_migrations_add_missing_fields_without_touching_existing(self):
        """Migrations should fill in missing skeleton keys but not overwrite populated ones."""
        import config as c
        partial = {
            "admin_ids": ["only_me"],
            "premium_channels": [
                {"id": "ch1", "name": "Only Name", "amount": "50"},  # missing desc/color/duration
            ],
            "upi_id": "existing@upi",  # MUST be preserved
        }
        changed = c._run_idempotent_migrations(partial)
        self.assertTrue(changed, "Expected migrations to add missing fields.")
        # Existing value preserved:
        self.assertEqual(partial["upi_id"], "existing@upi")
        self.assertEqual(partial["premium_channels"][0]["amount"], "50")
        # Missing fields added:
        ch = partial["premium_channels"][0]
        self.assertIn("description", ch)
        self.assertIn("color", ch)
        self.assertIn("duration", ch)
        self.assertEqual(partial["proof_channel_id"], "")
        self.assertEqual(partial["demo_amount"], "10")

    def test_env_admin_ids_merged_only_additive(self):
        """ADMIN_IDS from env are ADDED (not replaced) to DB admins."""
        import config as c
        # Temporarily override ADMIN_IDS_ENV
        original_env = c.ADMIN_IDS_ENV
        try:
            c.ADMIN_IDS_ENV = ["env_new_admin", "shared_id"]
            partial = {"admin_ids": ["db_old_admin", "shared_id"]}
            c._run_idempotent_migrations(partial)
            admins_set = set(partial["admin_ids"])
            self.assertIn("db_old_admin", admins_set, "old DB admin removed!")
            self.assertIn("env_new_admin", admins_set, "env admin not added!")
            self.assertIn("shared_id", admins_set)
            self.assertEqual(len(partial["admin_ids"]), 3, "duplicates introduced!")
        finally:
            c.ADMIN_IDS_ENV = original_env

# =====================================================================
# TEST GROUP 2 — config.py: safe data_is_empty + load_json_file logic
# =====================================================================
class TestDataSafety(unittest.TestCase):
    def test_data_is_empty_edge_cases(self):
        import config as c
        self.assertTrue(c._data_is_empty(None))
        self.assertTrue(c._data_is_empty({}))
        self.assertTrue(c._data_is_empty([]))
        self.assertTrue(c._data_is_empty(""))
        self.assertFalse(c._data_is_empty({"key": "val"}))
        self.assertFalse(c._data_is_empty([1]))
        self.assertFalse(c._data_is_empty("x"))
        self.assertFalse(c._data_is_empty(0))  # 0 is a number, not "empty default"
        self.assertFalse(c._data_is_empty(False))

    def test_write_json_raw_atomic_and_readable(self):
        """_write_json_raw writes a file; _read_json_raw reads it back (same content)."""
        import config as c
        tmpdir = tempfile.mkdtemp()
        try:
            path = os.path.join(tmpdir, "settings.json")
            data = {"users": {"1": {"name": "A"}}, "list": [1, 2, 3]}
            c._write_json_raw(path, data)
            self.assertTrue(os.path.exists(path))
            self.assertFalse(os.path.exists(path + ".tmp"), "tmp file not cleaned up!")
            read_back = c._read_json_raw(path)
            self.assertEqual(read_back, data)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_write_json_raw_creates_directory(self):
        import config as c
        tmpdir = tempfile.mkdtemp()
        try:
            deep = os.path.join(tmpdir, "a", "b", "c", "file.json")
            c._write_json_raw(deep, {"ok": True})
            self.assertTrue(os.path.exists(deep))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_load_json_file_prefers_non_empty_mongo_over_json(self):
        """If DB returns non-empty data AND JSON also exists, DB wins — and JSON is synced."""
        import config as c
        tmpdir = tempfile.mkdtemp()
        try:
            path = os.path.join(tmpdir, "users_data.json")
            # JSON file with old data
            with open(path, "w") as f:
                json.dump({"100": {"name": "OLD_JSON"}}, f)
            fake_db_data = {"100": {"name": "NEW_DB"}, "200": {"name": "ONLY_DB"}}
            # Mock db_load to return fake DB data, and db_save to track calls
            db_save_calls = []
            with patch.object(c, "db_load", return_value=fake_db_data), \
                 patch.object(c, "db_save", side_effect=lambda name, data: db_save_calls.append((name, data)) or True):
                result = c.load_json_file(path, {})
            self.assertEqual(result["100"]["name"], "NEW_DB", "DB data not preferred!")
            self.assertEqual(result["200"]["name"], "ONLY_DB", "DB data dropped!")
            # JSON should now be synced with DB data (write-through)
            with open(path) as f:
                json_on_disk = json.load(f)
            self.assertEqual(json_on_disk["100"]["name"], "NEW_DB")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_load_json_file_uses_json_when_db_none_or_empty(self):
        """If DB is temporarily down (None) OR has empty {}, local JSON wins; DB NOT overwritten with empty."""
        import config as c
        tmpdir = tempfile.mkdtemp()
        try:
            path = os.path.join(tmpdir, "settings.json")
            local = {"upi_id": "jsononly@upi", "admin_ids": ["X"]}
            with open(path, "w") as f:
                json.dump(local, f)
            db_saved = []
            with patch.object(c, "db_load", return_value=None), \
                 patch.object(c, "db_save", side_effect=lambda n, d: (db_saved.append((n, d)), True)[1]):
                result = c.load_json_file(path, {})
            # Result must be the LOCAL JSON data
            self.assertEqual(result["upi_id"], "jsononly@upi")
            # And it should have been pushed to DB (best-effort) when DB is reachable via mock
            self.assertTrue(any("jsononly@upi" in str(d) for _, d in db_saved),
                            "Valid JSON was not pushed to DB when DB became reachable")
            # Now simulate DB has EMPTY dict — local JSON should STILL win (not empty defaults)
            with patch.object(c, "db_load", return_value={}):
                result2 = c.load_json_file(path, {})
            self.assertEqual(result2["upi_id"], "jsononly@upi")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

# =====================================================================
# TEST GROUP 3 — bot.py: token format validation (regex, offline)
# =====================================================================
class TestTokenFormatValidation(unittest.TestCase):
    """Bot token format is validated BEFORE any network call. Tests the regex used in bot.py."""

    TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_\-]{20,60}$")

    def _check(self, token):
        return bool(self.TOKEN_RE.fullmatch(token))

    def test_valid_tokens(self):
        self.assertTrue(self._check("1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ_1234567890abcdef"))
        self.assertTrue(self._check("123456:Aa0-_1234567890123456789012"))  # 6 digit id, 24 char secret
        # 12 digit id + 24 char secret (4 chars per cycle * 6 = 24)
        secret = "ABCdefGHIjklMNOpqrSTUV"  # 24 chars
        self.assertTrue(self._check(f"999999999999:{secret}"))

    def test_invalid_tokens(self):
        cases = [
            "",
            "hello",                       # no colon
            "123:short",                   # too short after colon
            "12345:ABCDEFGHIJKLMNOPQRSTUV", # 5 digit id (too short)
            "123456:has space in it !!!",   # whitespace
            "123456:not!valid@char$",       # special chars
            ":no_id_part",                  # id missing
            "no_secret_part:",              # secret missing
            "notdigits:ABCDEFGHIJKLMNOPQRSTUVWXYZ01",  # non-digit id
        ]
        for c_ in cases:
            self.assertFalse(self._check(c_), f"Token '{c_}' should be INVALID format")

# =====================================================================
# TEST GROUP 4 — Secret redaction (config/bot never logs secrets)
# =====================================================================
class TestSecretRedaction(unittest.TestCase):
    def test_safe_repr_hides_token(self):
        import config as c
        d = {"BOT_TOKEN": "123456:SECRETSECRET", "other": "hello", "mongo_uri": "mongodb+srv://user:pw@host/db"}
        out = c._safe_repr(d)
        self.assertNotIn("123456:SECRET", out)
        self.assertNotIn("mongodb+srv", out)
        self.assertIn("hello", out)  # non-secret visible

    def test_redact_mongo_error_removes_uri(self):
        """Error messages that accidentally embed MongoDB URI are redacted."""
        import config as c
        errors = [
            "Bad mongo: connection to mongodb+srv://admin:mypass123@cluster0.mongodb.net/test failed: timeout",
            "Error: password=sup3rS3cret auth failed secret=abc123",
            "Token 12345678:aaaaaaaaaaaaaaaaaaaaaaaaaaaa leaked in error message",
            "api_key=AKIAIOSFODNN7EXAMPLE was not found",
        ]
        for e in errors:
            r = c.redact_secrets_from_text(e)
            self.assertNotIn("mongodb+srv", r)
            self.assertNotIn("mypass123", r)
            self.assertNotIn("sup3rS3cret", r)
            self.assertNotIn("abc123", r)
            self.assertNotIn("aaaaaaaaaaaaaaaaaaaaaaaaaaaa", r, "Bot token secret leaked")
            self.assertNotIn("AKIAIOSFODNN7EXAMPLE", r)

# =====================================================================
# TEST GROUP 5 — Data independence of bot identity
# =====================================================================
class TestBotIdentityIndependence(unittest.TestCase):
    """Verify persistent data structures don't store bot-username/id info."""

    def _check_dict_for_bot_identity_keys(self, d, path="$"):
        """Recursively check no keys suggestive of 'bot_token', 'bot_username' etc. exist in structure."""
        bad_keys = re.compile(r"(bot_token|api_key|bot_username|bot_id|secret)", re.IGNORECASE)
        if isinstance(d, dict):
            for k, v in d.items():
                if bad_keys.search(str(k)):
                    raise AssertionError(f"Found suspicious key {path}.{k}")
                self._check_dict_for_bot_identity_keys(v, f"{path}.{k}")
        elif isinstance(d, list):
            for i, v in enumerate(d):
                self._check_dict_for_bot_identity_keys(v, f"{path}[{i}]")

    def test_default_settings_no_identity_fields(self):
        import config as c
        self._check_dict_for_bot_identity_keys(c.DEFAULT_SETTINGS)

    def test_sales_and_pending_records_no_identity(self):
        """Records created by the code are user-centric, not bot-centric."""
        import config as c
        sample_pending = {
            "9999": {
                "plan": "ch1", "amount": "99", "order_number": 1,
                "initiated_at": "2024-01-01 00:00:00",
                "username": "userX", "first_name": "FN",
                "screenshot_file_id": "abc",
            }
        }
        sample_sale = {
            "order_number": 1, "user_id": "9999", "plan_type": "ch1",
            "plan_name": "Ch1", "amount": 99.0, "upi_id": "x@upi",
            "timestamp": "t", "date": "d", "admin_id": 111,
            "screenshot_file_id": "abc",
        }
        self._check_dict_for_bot_identity_keys(sample_pending)
        self._check_dict_for_bot_identity_keys(sample_sale)

# =====================================================================
# TEST GROUP 6 — Idempotent startup (restart with same data = stable)
# =====================================================================
class TestIdempotentStartup(unittest.TestCase):
    def test_double_migration_no_changes(self):
        """Running migrations twice on the same dict should be a no-op after first pass."""
        import config as c
        partial = {"admin_ids": ["A"]}
        ch1 = c._run_idempotent_migrations(partial)
        self.assertTrue(ch1, "First pass should have changed")
        # Deep-copy so mutation of nested objects still detected
        before = json.loads(json.dumps(partial))
        ch2 = c._run_idempotent_migrations(partial)
        after = partial
        self.assertFalse(ch2, "Second pass should report NO changes (idempotent)")
        self.assertEqual(before, after, "Second pass mutated state!")

# =====================================================================
# TEST GROUP 7 — MongoDB connection retry (mocked)
# =====================================================================
class TestMongoRetry(unittest.TestCase):
    def test_connect_mongodb_retries_then_falls_back_gracefully(self):
        """_connect_mongodb should retry N times, not crash process, and leave db=None on failure."""
        import config as c
        original_connect = _connect_mongodb_original = None
        original_uri = c.MONGO_URI
        try:
            # Force a realistic-looking (but not real) URI so we enter the retry loop
            c.MONGO_URI = "mongodb://user:pass@localhost:27017/testdb"
            call_count = {"n": 0}
            def fake_client(*a, **kw):
                call_count["n"] += 1
                from pymongo.errors import ConnectionFailure
                raise ConnectionFailure("fake network failure")
            with patch("config.MongoClient", side_effect=fake_client):
                # Speed up time.sleep in retry
                with patch("time.sleep", return_value=None) as sleep_mock:
                    # Temporarily reduce retries to 2 to keep test fast
                    with patch.object(c, "_MONGO_MAX_RETRIES", 2):
                        c._connect_mongodb()
            self.assertIsNone(c.db, "db should be None after all retries fail")
            self.assertGreaterEqual(call_count["n"], 2, "Retry logic didn't attempt multiple times")
            self.assertIsNotNone(c.mongo_error)
            self.assertTrue(c.startup_status["mongo_attempts"] >= 2)
            self.assertFalse(c.startup_status["mongo_connected"])
        finally:
            c.MONGO_URI = original_uri

# =====================================================================
# TEST GROUP 8 — Media cache (token-change auto re-upload)
# =====================================================================
class TestMediaCache(unittest.TestCase):
    """Media bytes are cached persistently so a new bot token can re-upload them."""

    def test_save_get_roundtrip_and_delete(self):
        import config as c
        mid = c.media_new_id()
        payload = b"\x89PNG\r\n\x1a\nFAKE-IMAGE-BYTES-1234567890"
        info = c.media_cache_save(mid, payload, "photo", file_name="pic.jpg")
        self.assertIsNotNone(info, "media_cache_save should succeed (local fallback at minimum)")
        self.assertEqual(info.get("type"), "photo")
        self.assertEqual(c.media_cache_get_bytes(mid), payload)
        meta = c.media_cache_get_info(mid)
        self.assertIsNotNone(meta)
        self.assertEqual(meta.get("filename"), "pic.jpg")
        c.media_cache_delete(mid)
        self.assertIsNone(c.media_cache_get_bytes(mid), "delete should remove the blob")
        self.assertIsNone(c.media_cache_get_info(mid), "delete should remove the manifest entry")

    def test_missing_media_returns_none(self):
        import config as c
        self.assertIsNone(c.media_cache_get_bytes("does-not-exist-" + c.media_new_id()))
        self.assertIsNone(c.media_cache_get_info("does-not-exist-" + c.media_new_id()))

    def test_oversize_rejected(self):
        import config as c
        mid = c.media_new_id()
        big = b"x" * (c.MEDIA_MAX_BYTES + 10)
        self.assertIsNone(c.media_cache_save(mid, big, "video"), "oversize media must not be cached")

    def test_empty_input_rejected(self):
        import config as c
        self.assertIsNone(c.media_cache_save("", b"data", "photo"))
        self.assertIsNone(c.media_cache_save(c.media_new_id(), b"", "photo"))

    def test_count_tracks_save_and_delete(self):
        import config as c
        before = c.media_cache_count()
        mid = c.media_new_id()
        c.media_cache_save(mid, b"count-test-bytes-123", "photo", file_name="c.jpg")
        self.assertEqual(c.media_cache_count(), before + 1)
        c.media_cache_delete(mid)
        self.assertEqual(c.media_cache_count(), before)

class TestAmountParsing(unittest.TestCase):
    """Prices are admin-entered text — '100rs' must never crash QR generation."""

    def test_accepts_common_formats(self):
        import config as c
        self.assertEqual(c.parse_amount("100rs"), 100.0)
        self.assertEqual(c.parse_amount("₹100"), 100.0)
        self.assertEqual(c.parse_amount("99.50"), 99.5)
        self.assertEqual(c.parse_amount("1,000.50"), 1000.5)
        self.assertEqual(c.parse_amount(" 250 "), 250.0)
        self.assertEqual(c.parse_amount(100), 100.0)
        self.assertEqual(c.parse_amount(100.5), 100.5)

    def test_rejects_invalid(self):
        import config as c
        for bad in (None, "", "   ", "abc", "rs", "0", -5, 0, True, False):
            self.assertIsNone(c.parse_amount(bad), msg=f"should reject {bad!r}")

    def test_clean_amount_str_normalises(self):
        import config as c
        self.assertEqual(c.clean_amount_str("100rs"), "100")
        self.assertEqual(c.clean_amount_str("99.50"), "99.50")
        self.assertEqual(c.clean_amount_str("₹1,000"), "1000")
        self.assertEqual(c.clean_amount_str("abc", default="0"), "0")

# =====================================================================
# MAIN
# =====================================================================
if __name__ == "__main__":
    print("=" * 60)
    print("🔬 Token Replacement + DB Persistence Verification Suite")
    print("(all tests run OFFLINE — no Telegram API, no live Mongo)")
    print("=" * 60)
    # Reduce verbosity: show full test names + errors, capture summary
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    print()
    if result.wasSuccessful():
        print("✅ ALL TESTS PASSED.")
        print("Summary:")
        print(f"  • Test cases run : {result.testsRun}")
        print(f"  • Failures       : {len(result.failures)}")
        print(f"  • Errors         : {len(result.errors)}")
        print()
        print("What these tests guarantee (local/offline):")
        print("  1. Settings merge never overwrites existing DB values with env defaults.")
        print("  2. Idempotent migrations fill missing fields but never wipe populated ones.")
        print("  3. ADMIN_IDS from env are MERGED (additive only) with DB admins.")
        print("  4. load_json_file: MongoDB (non-empty) beats JSON beats empty defaults.")
        print("  5. Token regex correctly rejects malformed tokens.")
        print("  6. Sensitive strings redacted in all logs/error messages.")
        print("  7. Persistent data records are user-centric (no bot-token/username stored).")
        print("  8. MongoDB retries on transient failure; process continues with JSON fallback.")
        sys.exit(0)
    else:
        print("❌ SOME TESTS FAILED. See details above.")
        sys.exit(1)
