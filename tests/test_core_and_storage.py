import os
import shutil
import tempfile
import unittest
from pathlib import Path

from cie import config, diagnostics, jobs, storage


class CoreAndStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cie_test_"))
        os.environ["CIE_HOME"] = str(self.tmp / "cie_home")
        os.environ["CIE_VAULT"] = str(self.tmp / "CIE-Vault")
        storage._DEFAULT_STORAGE = None
        jobs._JOB_MANAGER = None

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sqlite_fts5_and_result_cache(self) -> None:
        st = storage.get_storage()
        k, h = st.make_cache_key("test.op", "1.0", {"a": 1})
        self.assertIsNone(st.cache_get(k))
        st.cache_set(k, "test.op", "1.0", h, {"value": 42})
        got = st.cache_get(k)
        self.assertEqual(got, {"value": 42})
        stats = st.cache_stats()
        self.assertEqual(stats["summary"]["total_hits"], 1)

    def test_job_queue_and_interruption_recovery(self) -> None:
        st = storage.get_storage()
        st.execute(
            "INSERT INTO jobs(job_id, capability_id, status, progress, message, created_at, updated_at) VALUES ('stale1', 'x', 'running', 0.5, 'Old', '2026-01-01', '2026-01-01')"
        )
        jm = jobs.JobManager()
        stale = jm.status("stale1")
        self.assertEqual(stale["status"], "interrupted")

        submitted = jm.submit("core.diagnostics", lambda prog, ev: {"ok": True})
        res = None
        for _ in range(50):
            s = jm.status(submitted["job_id"])
            if s and s["status"] == "completed":
                res = s
                break
        self.assertIsNotNone(res)
        self.assertEqual(res["result"], {"ok": True})

    def test_completed_job_releases_in_memory_handles(self) -> None:
        import time
        jm = jobs.JobManager(max_workers=1)
        submitted = jm.submit("quick", lambda progress, cancel: {"done": True})
        deadline = time.time() + 2
        while time.time() < deadline:
            status = jm.status(submitted["job_id"])
            if status and status["status"] == "completed":
                break
            time.sleep(0.01)
        self.assertEqual(status["status"], "completed")
        self.assertNotIn(submitted["job_id"], jm._futures)
        self.assertNotIn(submitted["job_id"], jm._cancel_flags)

    def test_job_timeout_is_enforced_and_reported(self) -> None:
        import time
        jm = jobs.JobManager(max_workers=1)
        def cooperative_slow_job(progress, cancel):
            while not cancel.wait(0.01):
                pass
            return {"stopped": True}
        submitted = jm.submit("slow", cooperative_slow_job, timeout_sec=0.1)
        deadline = time.time() + 2
        status = None
        while time.time() < deadline:
            status = jm.status(submitted["job_id"])
            if status and status["status"] in {"timed_out", "completed", "failed"}:
                break
            time.sleep(0.01)
        self.assertEqual(status["status"], "timed_out")

    def test_diagnostics_self_check(self) -> None:
        chk = diagnostics.self_check()
        self.assertTrue(chk["healthy"])
        self.assertTrue(chk["sqlite_fts5"])
        self.assertTrue(chk["vault_writable"])


if __name__ == "__main__":
    unittest.main()