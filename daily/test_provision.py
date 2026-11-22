from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from daily.provision import get_request, request_for_desktop
from daily.storage import Store


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / "bridge.sqlite3"
        self.thread, self.id = str(uuid4()), str(uuid4())

    def submit(self, title="新 Claude"):
        return request_for_desktop(self.db, self.thread, self.id, title)

    def test_restart_and_concurrent_submit(self):
        self.assertIsNone(get_request(self.db, self.id))
        self.assertFalse(self.db.exists())
        with ThreadPoolExecutor(4) as pool:
            rows = list(pool.map(lambda _: self.submit(), range(8)))
        self.assertEqual(rows, [get_request(self.db, self.id)] * 8)
        self.assertEqual(rows[0]["state"], "queued")
        with self.assertRaisesRegex(ValueError, "another thread or title"):
            self.submit("Different")
        with self.assertRaisesRegex(ValueError, "another thread or title"):
            request_for_desktop(self.db, str(uuid4()), self.id, "新 Claude")
        with self.assertRaisesRegex(ValueError, "unresolved"):
            request_for_desktop(self.db, self.thread, str(uuid4()), "another")

    def test_existing_pair_preserved(self):
        store = Store(self.db)
        cc = dict(id=str(uuid4()), native_id=str(uuid4()), cwd="E:/lab", title="CC")
        binding = store.bind(cc, dict(id=self.thread, native_id=self.thread, cwd="E:/lab", title="Desktop"))
        with self.assertRaisesRegex(ValueError, "active binding"):
            self.submit()
        self.assertEqual([row["id"] for row in store.bindings()], [binding])
        self.assertIsNone(get_request(self.db, self.id))

    def test_cli_current_thread_only_and_gui_independent(self):
        cmd = [sys.executable, "-B", str(Path(__file__).with_name("provision.py")),
               "--store", str(self.db), "create", "--id", self.id, "--title", "new"]
        env = dict(os.environ)
        env.pop("CODEX_THREAD_ID", None)
        for overrides, extra in [({}, []), ({"CODEX_THREAD_ID": "bad"}, []),
                                 ({"CODEX_THREAD_ID": self.thread}, ["--desktop", str(uuid4())])]:
            result = subprocess.run(cmd + extra, env=env | overrides, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.db.exists())
        output = subprocess.check_output(cmd, env=env | {"CODEX_THREAD_ID": self.thread})
        row = json.loads(output)
        self.assertEqual(row["desktop"], self.thread)
        self.assertEqual(row, self.submit("new"))


if __name__ == "__main__":
    unittest.main()
