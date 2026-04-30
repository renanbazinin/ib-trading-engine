import json
import os
import tempfile
import unittest

from core.live_state_store import LiveStateStore


class LiveStateStoreTests(unittest.TestCase):
    def test_missing_file_reads_empty_dict(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = LiveStateStore(os.path.join(temp_dir, "live_state.json"))
            self.assertEqual(store.read(), {})

    def test_corrupt_file_reads_empty_dict(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "live_state.json")
            with open(path, "w", encoding="utf-8") as file_handle:
                file_handle.write("{not-json")
            store = LiveStateStore(path)
            self.assertEqual(store.read(), {})

    def test_write_is_readable_json_with_saved_at(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "live_state.json")
            store = LiveStateStore(path)
            store.write({"candles": [{"symbol": "TSLA"}], "logs": []})

            with open(path, "r", encoding="utf-8") as file_handle:
                raw = json.load(file_handle)

            self.assertEqual(raw["candles"][0]["symbol"], "TSLA")
            self.assertIn("saved_at", raw)
            self.assertEqual(store.read()["candles"][0]["symbol"], "TSLA")


if __name__ == "__main__":
    unittest.main()
