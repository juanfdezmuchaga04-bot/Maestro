"""Chunked uploads: limits, resume, integrity, streaming assembly, cleanup."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from services.upload_chunks import (  # noqa: E402
    DEFAULT_LIMITS,
    UploadSessionError,
    append_chunk,
    cancel_session,
    check_space,
    cleanup_sessions,
    complete_session,
    kind_limits,
    session_status,
    init_session,
)

KB = 1024
CHUNK = 64 * KB


class UploadTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self._tmp.name, "sessions")
        self.final = os.path.join(self._tmp.name, "uploads")

    def tearDown(self):
        self._tmp.cleanup()

    def payload(self, size):
        return bytes((index * 7) % 251 for index in range(size))

    def upload(self, data, *, filename="clip.mp4", chunk_size=CHUNK, **kwargs):
        session = init_session(
            self.root, filename=filename, total_bytes=len(data),
            chunk_size=chunk_size, **kwargs)
        for index in range(session["chunks_total"]):
            append_chunk(self.root, session["session_id"], index,
                         data[index * chunk_size:(index + 1) * chunk_size])
        return complete_session(self.root, session["session_id"], final_dir=self.final)


class LimitsTest(UploadTestCase):
    def test_limits_are_per_kind_and_configurable(self):
        limits = kind_limits()
        self.assertEqual(limits["image"], DEFAULT_LIMITS["image"])
        self.assertGreater(limits["video"], limits["image"])
        raised = kind_limits({"image": 500})
        self.assertEqual(raised["image"], 500)
        with self.assertRaises(UploadSessionError):
            kind_limits({"hologram": 1})
        with self.assertRaises(UploadSessionError):
            kind_limits({"image": 0})

    def test_a_file_over_its_kind_limit_is_refused_with_the_limit(self):
        with self.assertRaises(UploadSessionError) as ctx:
            init_session(
                self.root, filename="photo.png", total_bytes=1000,
                limits={"image": 100})
        message = str(ctx.exception)
        self.assertIn("image uploads are limited to", message)
        self.assertIn("configurable", message)

    def test_chunk_size_is_bounded(self):
        for bad in (1024, 512 * 1024 * 1024):
            with self.assertRaises(UploadSessionError) as ctx:
                init_session(self.root, filename="a.mp4", total_bytes=10, chunk_size=bad)
            self.assertIn("chunk_size", str(ctx.exception))

    def test_limits_are_derived_from_the_filename_extension(self):
        self.assertEqual(
            init_session(self.root, filename="a.wav", total_bytes=10)["kind"], "audio")
        self.assertEqual(
            init_session(self.root, filename="a.mov", total_bytes=10)["kind"], "video")
        self.assertEqual(
            init_session(self.root, filename="script.pdf", total_bytes=10)["kind"], "document")
        self.assertEqual(
            init_session(self.root, filename="blob", total_bytes=10)["kind"], "other")

    def test_the_filename_is_sanitised(self):
        session = init_session(self.root, filename="../../etc/passwd", total_bytes=5)
        self.assertEqual(session["filename"], "passwd")
        self.assertNotIn("..", session["filename"])

    def test_no_space_is_reported_before_anything_is_transferred(self):
        usage = mock.Mock(free=1024)
        with mock.patch("services.upload_chunks.shutil.disk_usage", return_value=usage):
            report = check_space(self.final, 10 * 1024)
            self.assertFalse(report["enough"])
            self.assertEqual(report["available_after_upload_bytes"], 0)
            with self.assertRaises(UploadSessionError) as ctx:
                init_session(self.root, filename="a.mp4", total_bytes=10 * 1024)
            self.assertIn("Not enough free space", str(ctx.exception))

    def test_an_invalid_expected_hash_is_refused_up_front(self):
        with self.assertRaises(UploadSessionError) as ctx:
            init_session(self.root, filename="a.png", total_bytes=4, expected_sha256="zz")
        self.assertIn("64-character hex", str(ctx.exception))


class ChunkedModeTest(UploadTestCase):
    def test_chunks_may_arrive_in_any_order_and_resume_is_reported(self):
        data = self.payload(CHUNK * 3)
        session = init_session(self.root, filename="a.mp4", total_bytes=len(data), chunk_size=CHUNK)
        self.assertEqual(session["chunks_total"], 3)
        self.assertEqual(session["mode"], "chunked")
        self.assertEqual(session["percent_complete"], 0.0)

        append_chunk(self.root, session["session_id"], 0, data[:CHUNK])
        append_chunk(self.root, session["session_id"], 2, data[2 * CHUNK:])
        state = session_status(self.root, session["session_id"])
        self.assertEqual(state["missing_chunks"], [1])
        self.assertEqual(state["received_chunks"], 2)
        self.assertGreater(state["percent_complete"], 60)

        with self.assertRaises(UploadSessionError) as ctx:
            complete_session(self.root, session["session_id"], final_dir=self.final)
        self.assertIn("1 chunk(s) missing, starting at 1", str(ctx.exception))

        append_chunk(self.root, session["session_id"], 1, data[CHUNK:2 * CHUNK])
        result = complete_session(self.root, session["session_id"], final_dir=self.final)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["percent_complete"], 100.0)
        self.assertEqual(result["missing_chunks"], [])
        with open(result["final_path"], "rb") as handle:
            self.assertEqual(handle.read(), data)

    def test_re_sending_a_chunk_is_idempotent(self):
        data = self.payload(CHUNK * 2)
        session = init_session(self.root, filename="a.mp4", total_bytes=len(data), chunk_size=CHUNK)
        append_chunk(self.root, session["session_id"], 0, data[:CHUNK])
        append_chunk(self.root, session["session_id"], 0, data[:CHUNK])
        state = session_status(self.root, session["session_id"])
        self.assertEqual(state["received_bytes"], CHUNK)
        self.assertEqual(state["missing_chunks"], [1])

    def test_an_out_of_range_chunk_is_refused(self):
        session = init_session(self.root, filename="a.mp4", total_bytes=CHUNK, chunk_size=CHUNK)
        with self.assertRaises(UploadSessionError) as ctx:
            append_chunk(self.root, session["session_id"], 5, b"x")
        self.assertIn("out of range", str(ctx.exception))

    def test_a_chunk_larger_than_the_session_chunk_size_is_refused(self):
        session = init_session(self.root, filename="a.mp4", total_bytes=CHUNK * 2, chunk_size=CHUNK)
        with self.assertRaises(UploadSessionError) as ctx:
            append_chunk(self.root, session["session_id"], 0, b"z" * (CHUNK + 1))
        self.assertIn("chunk size", str(ctx.exception))

    def test_the_chunk_count_is_complete_even_for_a_partial_last_chunk(self):
        data = self.payload(CHUNK + 10)
        session = init_session(self.root, filename="a.mp4", total_bytes=len(data), chunk_size=CHUNK)
        self.assertEqual(session["chunks_total"], 2)
        result = self.upload(data)
        with open(result["final_path"], "rb") as handle:
            self.assertEqual(len(handle.read()), len(data))


class StreamingModeTest(UploadTestCase):
    def test_an_unknown_size_requires_in_order_chunks(self):
        session = init_session(self.root, filename="rec.webm", total_bytes=0, chunk_size=CHUNK)
        self.assertEqual(session["mode"], "streaming")
        self.assertIsNone(session["chunks_total"])
        append_chunk(self.root, session["session_id"], 0, b"first")
        with self.assertRaises(UploadSessionError) as ctx:
            append_chunk(self.root, session["session_id"], 4, b"later")
        self.assertIn("must arrive in order", str(ctx.exception))
        append_chunk(self.root, session["session_id"], 1, b"second")
        result = complete_session(self.root, session["session_id"], final_dir=self.final)
        with open(result["final_path"], "rb") as handle:
            self.assertEqual(handle.read(), b"firstsecond")

    def test_a_session_with_no_chunks_cannot_be_completed(self):
        session = init_session(self.root, filename="rec.webm", total_bytes=0)
        with self.assertRaises(UploadSessionError) as ctx:
            complete_session(self.root, session["session_id"], final_dir=self.final)
        self.assertIn("No chunks were received", str(ctx.exception))


class IntegrityTest(UploadTestCase):
    def test_a_chunk_that_fails_its_own_hash_is_rejected_and_not_stored(self):
        import hashlib

        session = init_session(self.root, filename="a.mp4", total_bytes=CHUNK, chunk_size=CHUNK)
        with self.assertRaises(UploadSessionError) as ctx:
            append_chunk(self.root, session["session_id"], 0, b"payload",
                         chunk_sha256=hashlib.sha256(b"other").hexdigest())
        self.assertIn("integrity check", str(ctx.exception))
        self.assertEqual(session_status(self.root, session["session_id"])["received_chunks"], 0)
        good = hashlib.sha256(b"payload").hexdigest()
        append_chunk(self.root, session["session_id"], 0, b"payload", chunk_sha256=good)
        self.assertEqual(session_status(self.root, session["session_id"])["received_bytes"], 7)

    def test_the_assembled_file_must_match_the_declared_hash(self):
        data = self.payload(CHUNK)
        session = init_session(
            self.root, filename="a.mp4", total_bytes=len(data), chunk_size=CHUNK,
            expected_sha256="0" * 64)
        append_chunk(self.root, session["session_id"], 0, data)
        with self.assertRaises(UploadSessionError) as ctx:
            complete_session(self.root, session["session_id"], final_dir=self.final)
        self.assertIn("does not match the SHA-256", str(ctx.exception))
        state = session_status(self.root, session["session_id"])
        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["error"], "Assembled file did not match the declared SHA-256")
        # The session is kept so the client can re-send, but no artifact is left.
        self.assertEqual(os.listdir(self.final), [])

    def test_a_correct_declared_hash_completes(self):
        import hashlib

        data = self.payload(CHUNK + 5)
        result = self.upload(data, expected_sha256=hashlib.sha256(data).hexdigest())
        self.assertEqual(result["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(result["state"], "completed")


class DedupAndCleanupTest(UploadTestCase):
    def test_identical_content_is_reused(self):
        data = self.payload(CHUNK)
        first = self.upload(data, filename="a.mp4")
        second = self.upload(data, filename="b.mp4")
        self.assertFalse(first["deduplicated"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(second["final_path"], first["final_path"])
        self.assertEqual(len(os.listdir(self.final)), 1)

    def test_completing_removes_the_temporary_chunks(self):
        data = self.payload(CHUNK * 2)
        result = self.upload(data)
        directory = os.path.join(self.root, result["session_id"])
        leftovers = [name for name in os.listdir(directory) if name.startswith("chunk-")]
        self.assertEqual(leftovers, [])
        self.assertTrue(os.path.isfile(os.path.join(directory, "session.json")))

    def test_cancel_deletes_temporary_data_and_reports_the_state(self):
        session = init_session(self.root, filename="a.mp4", total_bytes=CHUNK * 2, chunk_size=CHUNK)
        append_chunk(self.root, session["session_id"], 0, b"x" * CHUNK)
        cancelled = cancel_session(self.root, session["session_id"])
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(cancelled["received_bytes"], 0)
        directory = os.path.join(self.root, session["session_id"])
        self.assertEqual([n for n in os.listdir(directory) if n.startswith("chunk-")], [])
        with self.assertRaises(UploadSessionError) as ctx:
            append_chunk(self.root, session["session_id"], 1, b"y")
        self.assertIn("no longer accepts chunks", str(ctx.exception))

    def test_removing_a_cancelled_session_frees_its_directory(self):
        session = init_session(self.root, filename="a.mp4", total_bytes=CHUNK, chunk_size=CHUNK)
        append_chunk(self.root, session["session_id"], 0, b"x" * CHUNK)
        result = cancel_session(self.root, session["session_id"], keep_record=False)
        self.assertTrue(result["removed"])
        self.assertFalse(os.path.isdir(os.path.join(self.root, session["session_id"])))

    def test_cleanup_drops_stale_and_finished_sessions(self):
        finished = self.upload(self.payload(CHUNK))
        stale = init_session(self.root, filename="old.mp4", total_bytes=CHUNK,
                             chunk_size=CHUNK, now=0.0)
        append_chunk(self.root, stale["session_id"], 0, b"z" * CHUNK, now=0.0)
        fresh = init_session(self.root, filename="new.mp4", total_bytes=CHUNK, chunk_size=CHUNK)

        report = cleanup_sessions(self.root, older_than=3600, now=100_000.0)
        self.assertIn(finished["session_id"], report["removed"])
        # An in-progress upload is never deleted by default: that would be data
        # loss. The caller has to ask for it explicitly.
        self.assertNotIn(stale["session_id"], report["removed"])
        self.assertGreater(report["freed_bytes"], 0)
        self.assertTrue(os.path.isdir(os.path.join(self.root, fresh["session_id"])))

        report = cleanup_sessions(self.root, older_than=3600, now=100_000.0, include_open=True)
        self.assertIn(stale["session_id"], report["removed"])
        self.assertTrue(os.path.isdir(os.path.join(self.root, fresh["session_id"])))


class SessionLookupTest(UploadTestCase):
    def test_an_unknown_or_traversing_session_id_is_refused(self):
        for bad in ("nope", "../etc", "", "A" * 32):
            with self.assertRaises(UploadSessionError):
                session_status(self.root, bad)

    def test_a_corrupt_session_file_is_reported(self):
        session = init_session(self.root, filename="a.png", total_bytes=4)
        path = os.path.join(self.root, session["session_id"], "session.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{broken")
        with self.assertRaises(UploadSessionError) as ctx:
            session_status(self.root, session["session_id"])
        self.assertIn("corrupt", str(ctx.exception))

    def test_completing_twice_is_harmless(self):
        data = self.payload(CHUNK)
        session = init_session(self.root, filename="a.mp4", total_bytes=len(data), chunk_size=CHUNK)
        append_chunk(self.root, session["session_id"], 0, data)
        first = complete_session(self.root, session["session_id"], final_dir=self.final)
        second = complete_session(self.root, session["session_id"], final_dir=self.final)
        self.assertEqual(second["final_path"], first["final_path"])
        self.assertEqual(second["state"], "completed")


if __name__ == "__main__":
    unittest.main()
