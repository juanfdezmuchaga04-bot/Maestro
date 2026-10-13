"""The chunked upload HTTP surface, driven the way its two callers use it.

Two different clients reach these routes: the browser (Studio's transfer panel
reads ``/api/v1/uploads/limits`` and posts a large reference through a session) and
another Maestro sending the media a delegated request brings
(``remote_engine.transfer_media``).  The router is therefore served here directly
with httpx, so the paths and the status codes both callers depend on stay put.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from services import upload_api  # noqa: E402
from services import upload_chunks  # noqa: E402


class UploadApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.sessions = os.path.join(self._tmp.name, "sessions")
        self.uploads = os.path.join(self._tmp.name, "uploads")
        app = FastAPI()
        app.include_router(
            upload_api.create_router(
                sessions_root_provider=lambda: self.sessions,
                uploads_root_provider=lambda: self.uploads,
            )
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://maestro.test"
        )
        self.addAsyncCleanup(self.client.aclose)

    async def send(self, data: bytes, *, filename: str = "reference.png") -> dict:
        """One whole file through the real session endpoints."""
        opened = await self.client.post(
            "/api/v1/uploads/sessions",
            json={
                "filename": filename,
                "total_bytes": len(data),
                "chunk_size": 64 * 1024,
            },
        )
        self.assertEqual(opened.status_code, 200, opened.text)
        session = opened.json()
        session_id = session["session_id"]
        step = int(session["chunk_size"])
        index = 0
        for offset in range(0, len(data), step):
            block = data[offset:offset + step]
            chunk = await self.client.post(
                f"/api/v1/uploads/sessions/{session_id}/chunks?index={index}",
                content=block,
                headers={"Content-Type": "application/octet-stream"},
            )
            self.assertEqual(chunk.status_code, 200, chunk.text)
            index += 1
        done = await self.client.post(
            f"/api/v1/uploads/sessions/{session_id}/complete", json={"dedup": True}
        )
        self.assertEqual(done.status_code, 200, done.text)
        return done.json()

    async def test_the_limits_the_interface_reads_are_served(self):
        response = await self.client.get("/api/v1/uploads/limits")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["limits"], upload_chunks.kind_limits())
        self.assertEqual(body["chunk_size"], upload_chunks.DEFAULT_CHUNK_SIZE)
        self.assertGreater(body["limits"]["video"], body["limits"]["image"])

    async def test_a_file_arrives_whole_and_is_reused_when_it_is_sent_again(self):
        payload = bytes((index * 13) % 251 for index in range(200 * 1024))
        first = await self.send(payload)
        self.assertEqual(first["state"], "completed")
        self.assertTrue(os.path.isabs(first["final_path"]))
        self.assertEqual(os.path.dirname(first["final_path"]), os.path.realpath(self.uploads))
        with open(first["final_path"], "rb") as handle:
            self.assertEqual(handle.read(), payload)
        # The peer deduplicates by content, so a second attempt of the same file
        # does not create a second copy — which is what keeps a re-sent reference
        # from filling the other computer's disk.
        second = await self.send(payload)
        self.assertEqual(second["final_path"], first["final_path"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(os.listdir(self.uploads), [os.path.basename(first["final_path"])])

    async def test_the_session_status_is_the_resume_contract(self):
        data = b"x" * (64 * 1024 * 2 + 10)
        opened = await self.client.post(
            "/api/v1/uploads/sessions",
            json={"filename": "clip.mp4", "total_bytes": len(data), "chunk_size": 64 * 1024},
        )
        session_id = opened.json()["session_id"]
        await self.client.post(
            f"/api/v1/uploads/sessions/{session_id}/chunks?index=1",
            content=data[64 * 1024:128 * 1024],
        )
        status = await self.client.get(f"/api/v1/uploads/sessions/{session_id}")
        self.assertEqual(status.status_code, 200)
        body = status.json()
        self.assertEqual(body["chunks_total"], 3)
        self.assertEqual(body["missing_chunks"], [0, 2])
        self.assertEqual(body["state"], "open")

    async def test_an_unknown_session_is_a_404_and_a_silly_chunk_a_400(self):
        missing = await self.client.get("/api/v1/uploads/sessions/deadbeefdeadbeefdeadbeefdeadbeef")
        self.assertEqual(missing.status_code, 404)
        malformed = await self.client.get("/api/v1/uploads/sessions/not-a-session-id")
        self.assertEqual(malformed.status_code, 400)
        opened = await self.client.post(
            "/api/v1/uploads/sessions",
            json={"filename": "clip.mp4", "total_bytes": 128 * 1024, "chunk_size": 64 * 1024},
        )
        session_id = opened.json()["session_id"]
        too_big = await self.client.post(
            f"/api/v1/uploads/sessions/{session_id}/chunks?index=0",
            content=b"y" * (200 * 1024),
        )
        self.assertEqual(too_big.status_code, 413)
        self.assertIn("limit for this session", too_big.json()["detail"])

    async def test_an_upload_over_its_kind_limit_is_refused_with_the_limit(self):
        opened = await self.client.post(
            "/api/v1/uploads/sessions",
            json={
                "filename": "huge.png",
                "total_bytes": upload_chunks.DEFAULT_LIMITS["image"] + 1,
                "chunk_size": 64 * 1024,
            },
        )
        self.assertEqual(opened.status_code, 400)
        self.assertIn("image uploads are limited", opened.json()["detail"])


if __name__ == "__main__":
    unittest.main()
