"""A stub ComfyUI server that speaks the real protocol, including video output.

Shared by the local-engine tests.  It implements the documented endpoints
(``/system_stats``, ``/object_info``, ``/prompt``, ``/history/{id}``, ``/queue``,
``/interrupt``, ``/view``) with the Wan 2.2 node classes the shipped template
needs, so the adapter, the bootstrap and the queue handler can all be exercised
over a real socket.

What it does **not** do is generate anything: it returns a small but genuine MP4
(real ``ftyp`` box) so the "is this a usable video file" check has something real
to accept and genuinely rejects.  Nothing in the tests claims a model ran.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# The node classes the shipped Wan 2.2 template uses.
WAN_NODE_CLASSES = [
    "UNETLoader",
    "CLIPLoader",
    "VAELoader",
    "ModelSamplingSD3",
    "CLIPTextEncode",
    "Wan22ImageToVideoLatent",
    "KSampler",
    "VAEDecode",
    "CreateVideo",
    "SaveVideo",
]

WAN_FILES = {
    "diffusion_models": ["wan2.2_ti2v_5B_fp16.safetensors"],
    "text_encoders": ["umt5_xxl_fp8_e4m3fn_scaled.safetensors"],
    "vae": ["wan2.2_vae.safetensors"],
}

# A minimal but structurally real MP4: 4-byte size + 'ftyp' + brand + filler.
FAKE_MP4 = (32).to_bytes(4, "big") + b"ftypisom" + b"\x00\x00\x02\x00isomiso2" + b"\x00" * 2048


class StubComfy(BaseHTTPRequestHandler):
    node_classes = list(WAN_NODE_CLASSES)
    files = dict(WAN_FILES)
    submitted: list = []
    interrupts = 0
    deleted: list = []
    finished = True          # when False, /history is empty and /queue reports running
    output_key = "videos"    # the key the node's output list arrives under
    video_filename = "maestro_00001_.mp4"
    view_bytes = FAKE_MP4
    view_missing = False

    def log_message(self, *args):  # keep the test output readable
        return

    def _json(self, payload, code=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, payload, code=200, content_type="video/mp4"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/system_stats":
            return self._json(
                {
                    "system": {"comfyui_version": "0.39.0", "python_version": "3.12.4"},
                    "devices": [
                        {"name": "cuda:0", "type": "cuda", "vram_total": 8589, "vram_free": 7800},
                    ],
                }
            )
        if path == "/object_info":
            info = {cls: {"input": {"required": {}}} for cls in type(self).node_classes}
            info["UNETLoader"] = {
                "input": {"required": {"unet_name": [list(type(self).files["diffusion_models"])], "weight_dtype": [["default"]]}}
            }
            info["CLIPLoader"] = {
                "input": {"required": {"clip_name": [list(type(self).files["text_encoders"])], "type": [["wan", "sdxl"]]}}
            }
            info["VAELoader"] = {"input": {"required": {"vae_name": [list(type(self).files["vae"])]}}}
            return self._json(info)
        if path == "/queue":
            if type(self).finished:
                return self._json({"queue_running": [], "queue_pending": []})
            prompt_id = type(self).submitted[-1]["prompt_id"] if type(self).submitted else ""
            return self._json({"queue_running": [["1", prompt_id]], "queue_pending": []})
        if path.startswith("/history/"):
            prompt_id = path.rsplit("/", 1)[-1]
            if not type(self).finished:
                return self._json({})
            return self._json(
                {
                    prompt_id: {
                        "status": {"status_str": "success", "completed": True, "messages": []},
                        "outputs": {
                            "11": {
                                type(self).output_key: [
                                    {
                                        "filename": type(self).video_filename,
                                        "subfolder": "",
                                        "type": "output",
                                    }
                                ]
                            }
                        },
                    }
                }
            )
        if path == "/view":
            if type(self).view_missing:
                return self._json({"error": "not found"}, code=404)
            return self._bytes(type(self).view_bytes)
        return self._json({"error": "not found"}, code=404)

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        if path == "/prompt":
            graph = payload.get("prompt") or {}
            missing = [
                node.get("class_type")
                for node in graph.values()
                if node.get("class_type") not in type(self).node_classes
            ]
            if missing:
                return self._json({"error": "missing nodes", "node_errors": {"1": missing}}, code=400)
            prompt_id = f"pid{len(type(self).submitted) + 1}"
            type(self).submitted.append({**payload, "prompt_id": prompt_id})
            return self._json({"prompt_id": prompt_id, "number": 1})
        if path == "/interrupt":
            type(self).interrupts += 1
            return self._json({})
        if path == "/queue":
            type(self).deleted.extend(payload.get("delete") or [])
            return self._json({})
        if path == "/upload/image":
            return self._json({"name": "uploaded.png", "subfolder": "", "type": "input"})
        return self._json({"error": "not found"}, code=404)


def reset_stub() -> None:
    StubComfy.node_classes = list(WAN_NODE_CLASSES)
    StubComfy.files = {key: list(value) for key, value in WAN_FILES.items()}
    StubComfy.submitted = []
    StubComfy.interrupts = 0
    StubComfy.deleted = []
    StubComfy.finished = True
    StubComfy.output_key = "videos"
    StubComfy.video_filename = "maestro_00001_.mp4"
    StubComfy.view_bytes = FAKE_MP4
    StubComfy.view_missing = False


class RunningStub:
    """Context manager: serve the stub on a free port for the duration of a test."""

    def __init__(self, handler=StubComfy):
        self.handler = handler
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "RunningStub":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def submitted_graphs(self) -> list:
        return list(self.handler.submitted)

    def last_params(self) -> dict:
        graphs = self.submitted_graphs()
        return graphs[-1]["prompt"] if graphs else {}


def query_of(url: str) -> dict:
    return {key: value[0] for key, value in parse_qs(urlparse(url).query).items()}
