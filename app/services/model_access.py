"""Every way to reach each model Maestro ships — free APIs, trials, public Spaces, local backends.

Researched 2026-10-14.  Each route records *what kind* of free it is, the limit as
published, the page it was read from and how sure we are:

* ``official``  — read from the provider's own documentation or model card;
* ``secondary`` — reported by third-party pages that agree with each other;
* ``unverified`` — a single or conflicting report; shown so the user can check it,
  never presented as a fact.

Kinds of access (``ROUTE_KINDS``) keep "free" honest: a recurring free quota is not
the same thing as one-time trial credits, and a public demo Space is not an API
contract.  Paid routes are listed too, as reference, and are marked as such.

Licences matter as much as price: HunyuanVideo and MiniMax H3 weights exclude the
European Union (and other territories) from their licence, FLUX.1 [dev] /
FLUX.2 [dev] / FLUX.2 [klein] 9B are non-commercial, and Qwen-Image 2.1 is research
only.  :data:`FAMILIES` carries that next to the model so a download can require
the user to acknowledge it.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

__all__ = ["RESEARCHED_ON", "ROUTE_KINDS", "ROUTES", "FAMILIES", "family_for", "access_for",
           "local_feasibility", "LOCAL_BACKENDS"]

RESEARCHED_ON = "2026-10-14"

ROUTE_KINDS = {
    "free_recurring": "Free quota that renews (daily/monthly) without payment",
    "free_web": "Free web app from the model's authors (no programmatic API confirmed)",
    "public_space": "Public demo Space (Gradio); usable through Maestro's public compute, limited GPU minutes",
    "trial_credits": "One-time or promotional credits; payment needed afterwards",
    "free_gpu_notebook": "Free NVIDIA GPU notebook: run the model yourself inside the notebook session",
    "paid": "Paid API (reference only)",
    "local": "Runs on your own hardware with the downloaded weights",
}

# ---------------------------------------------------------------------------
# Services / systems
# ---------------------------------------------------------------------------

ROUTES: dict[str, dict[str, Any]] = {
    "hf_zerogpu": {
        "label": "Hugging Face ZeroGPU Spaces", "kind": "public_space",
        "limits": "About 2 GPU-minutes/day anonymous, 5 with a free account, 40 with PRO; shared queue",
        "account": "None needed (a free HF account raises the quota)",
        "maestro": "Integrated: Studio → Compute & transfer → Public compute (automatic discovery)",
        "source": "https://huggingface.co/docs/hub/en/spaces-zerogpu", "confidence": "official",
    },
    "hf_inference_providers": {
        "label": "Hugging Face Inference Providers", "kind": "free_recurring",
        "limits": "$0.10 of credits per month for free accounts (PRO: $2.00); no pay-as-you-go on the free plan",
        "account": "Free Hugging Face account + access token",
        "maestro": "Not integrated",
        "source": "https://huggingface.co/docs/inference-providers/pricing", "confidence": "official",
    },
    "cloudflare": {
        "label": "Cloudflare Workers AI", "kind": "free_recurring",
        "limits": "10,000 Neurons/day free, reset 00:00 UTC (FLUX.1 schnell ≈ 58 Neurons per 1024² image; "
                  "Whisper large-v3-turbo ≈ 47 Neurons per audio minute; MeloTTS ≈ 19 per minute)",
        "account": "Cloudflare account (Free plan, no card) + API token + Account ID",
        "maestro": "Integrated (Cloud mode): FLUX.1 [schnell], FLUX.2 [klein] 4B and 9B",
        "source": "https://developers.cloudflare.com/workers-ai/platform/pricing/", "confidence": "official",
    },
    "modelscope": {
        "label": "ModelScope API-Inference", "kind": "free_recurring",
        "limits": "Historically 2,000 calls/day (UTC+8 reset); a Sept-2026 report shows a balance-based "
                  "system instead. Non-commercial experimentation, limited concurrency",
        "account": "ModelScope account linked to a real-name-verified Alibaba Cloud account + ModelScope token",
        "maestro": "Integrated (Cloud mode): Qwen-Image 20B, Z-Image Turbo, Qwen-Image 2.1",
        "source": "https://lilting.ch/en/articles/modelscope-api-inference-magicube-probe", "confidence": "secondary",
    },
    "google_ai_studio": {
        "label": "Google AI Studio (Gemini API)", "kind": "free_recurring",
        "limits": "Gemma models are free on the API; Gemma 4 26B/31B reported at 1,500 requests/day. "
                  "Free-tier content may be used to improve Google products",
        "account": "Google account + Gemini API key",
        "maestro": "Not integrated (Maestro's LLM can use an OpenAI-compatible endpoint)",
        "source": "https://aistudio.google.com/rate-limit", "confidence": "unverified",
    },
    "groq": {
        "label": "Groq", "kind": "free_recurring",
        "limits": "whisper-large-v3 / -turbo free: 20 req/min, 2,000 req/day, 28,800 audio-seconds/day, 25 MB files",
        "account": "Groq account + API key (no card)",
        "maestro": "Not integrated",
        "source": "https://console.groq.com/docs/speech-to-text", "confidence": "secondary",
    },
    "openrouter_free": {
        "label": "OpenRouter :free models", "kind": "free_recurring",
        "limits": "20 req/min; 50 req/day (1,000/day after buying $10 of credit). The free list rotates",
        "account": "OpenRouter account + key",
        "maestro": "Usable as a custom OpenAI-compatible LLM endpoint",
        "source": "https://benchlm.ai/md/free-tier/openrouter.md", "confidence": "secondary",
    },
    "vercel_ai_gateway": {
        "label": "Vercel AI Gateway", "kind": "free_recurring",
        "limits": "$5 of credits every 30 days for accounts that never paid; lists FLUX.2 [klein] 4B and 9B",
        "account": "Vercel account",
        "maestro": "Not integrated",
        "source": "https://vercel.com/ai-gateway/models/flux-2-klein-9b/playground", "confidence": "secondary",
    },
    "pollinations": {
        "label": "Pollinations.AI", "kind": "free_recurring",
        "limits": "Anonymous ≈ 1 request / 15 s (FLUX images); more with a free key; fair use",
        "account": "None (optional free key)",
        "maestro": "Not integrated",
        "source": "https://agentdeals.dev/vendor/pollinations-ai", "confidence": "secondary",
    },
    "nvidia_build": {
        "label": "NVIDIA build.nvidia.com (NIM trial)", "kind": "trial_credits",
        "limits": "About 1,000 API credits on joining the free Developer Program (up to 5,000 reported), 40 req/min",
        "account": "NVIDIA Developer Program account (no card)",
        "maestro": "Not integrated",
        "source": "https://forums.developer.nvidia.com/t/api-credits-for-build-nvidia-com/306633", "confidence": "secondary",
    },
    "alibaba_model_studio": {
        "label": "Alibaba Cloud Model Studio (DashScope)", "kind": "trial_credits",
        "limits": "New-user free quota per model, visible in the console's Free Quota tab with its expiry; "
                  "whether it covers Wan video / Qwen-Image / Qwen3-TTS is not stated in the docs",
        "account": "Alibaba Cloud account",
        "maestro": "Not integrated",
        "source": "https://help.aliyun.com/en/model-studio/new-free-quota", "confidence": "official",
    },
    "siliconflow": {
        "label": "SiliconFlow", "kind": "free_recurring",
        "limits": "A rotating set of $0 models (Kolors image, small Qwen LLMs) + ≈ $1 starter credit; Wan video is paid",
        "account": "SiliconFlow account (real-name verification since May 2026; servers in China)",
        "maestro": "Not integrated",
        "source": "https://pricepertoken.com/endpoints/siliconflow/free", "confidence": "secondary",
    },
    "kaggle_notebook": {
        "label": "Kaggle Notebooks (free NVIDIA GPU)", "kind": "free_gpu_notebook",
        "limits": "T4 ×2 (16 GB each) or P100; ≈ 30 GPU-hours/week; sessions up to 9–12 h; ≈ 20 GB persistent disk",
        "account": "Free Kaggle account, phone-verified for GPU and internet",
        "maestro": "Not integrated: run the model in the notebook (personal, non-commercial use per Kaggle's terms)",
        "source": "https://www.kaggle.com/docs/notebooks", "confidence": "secondary",
    },
    "lightning_studio": {
        "label": "Lightning AI Studios (free monthly credits)", "kind": "free_gpu_notebook",
        "limits": "Free monthly credits reported ≈ 80 GPU-hours on T4; phone verification, no card reported",
        "account": "Free Lightning AI account",
        "maestro": "Not integrated: run the model in the Studio",
        "source": "https://lightning.ai/pricing/", "confidence": "secondary",
    },
    "acemusic": {
        "label": "acemusic.ai (ACE-Step authors)", "kind": "free_web",
        "limits": "Described by the ACE-Step README as '100% free'; no API documented",
        "account": "Web account", "maestro": "Not integrated",
        "source": "https://github.com/ACE-Step/ACE-Step-1.5", "confidence": "official",
    },
    "replicate": {
        "label": "Replicate", "kind": "paid",
        "limits": "Pay per output (FLUX schnell ≈ $0.003/image, Wan 2.2 Fast ≈ $0.05–0.10/video)",
        "account": "Account with billing", "maestro": "Integrated (Cloud mode, opt-in)",
        "source": "https://replicate.com/docs/topics/billing", "confidence": "official",
    },
    "fal": {
        "label": "fal.ai", "kind": "paid",
        "limits": "Pay per output (Kandinsky 5 ≈ $0.08/5 s; HunyuanVideo 1.5, Wan, LTX, FLUX, Qwen-Image…)",
        "account": "Account with billing", "maestro": "Not integrated",
        "source": "https://fal.ai/models/fal-ai/kandinsky5/text-to-video/api", "confidence": "official",
    },
    "minimax_api": {
        "label": "MiniMax platform API", "kind": "paid",
        "limits": "H3 billed per second (≈ $0.13/s via OpenRouter)", "account": "MiniMax account with billing",
        "maestro": "Not integrated",
        "source": "https://blog.segmind.com/minimax-h3-release-date-open-weights-and-api-pricing-explained/",
        "confidence": "secondary",
    },
    "ltx_api": {
        "label": "LTX API (Lightricks)", "kind": "paid",
        "limits": "Prepaid credits, minimum purchase $5; billed by video duration",
        "account": "LTX account", "maestro": "Not integrated",
        "source": "https://help.ltx.io/hc/en-us/articles/32478713737618", "confidence": "official",
    },
    "bfl_api": {
        "label": "Black Forest Labs API", "kind": "paid",
        "limits": "Per megapixel; FLUX.2 [klein] 4B from ≈ $0.014/image; no signup credits confirmed",
        "account": "BFL account with credits", "maestro": "Not integrated",
        "source": "https://help.bfl.ai/articles/7424166364-wip-welcome-to-the-forest", "confidence": "official",
    },
}

# Local engines that do not need NVIDIA.  "Maestro" means Maestro can drive it today.
LOCAL_BACKENDS: dict[str, dict[str, Any]] = {
    "wan2gp_cuda": {"label": "Maestro engine (Wan2GP)", "hardware": "NVIDIA CUDA",
                    "maestro": "Built in", "source": "README.md", "confidence": "official"},
    "comfyui": {"label": "ComfyUI", "hardware": "NVIDIA CUDA · AMD ROCm (Windows Desktop ≥ v0.7.0 and Linux, supported "
                "Radeon/Ryzen AI only) · Apple Silicon (MPS) · CPU (--cpu, very slow)",
                "maestro": "Adapter exists (services/comfyui_adapter.py); Wan 2.2 5B template shipped",
                "source": "https://www.amd.com/en/blogs/2026/amd-comfyui-advancing-professional-quality-generative-ai-ryzen-radeon.html",
                "confidence": "official"},
    "sdcpp": {"label": "stable-diffusion.cpp", "hardware": "CPU · Vulkan (AMD/Intel/NVIDIA) · Metal · OpenCL · SYCL",
              "models": "FLUX.1/FLUX.2 (dev, klein), Wan 2.1/2.2, LTX-2.3, HunyuanVideo 1.5, Qwen-Image / Edit (GGUF)",
              "maestro": "Not integrated", "source": "https://github.com/leejet/stable-diffusion.cpp",
              "confidence": "secondary"},
    "llama_cpp": {"label": "llama.cpp", "hardware": "CPU · Vulkan · Metal · CUDA · ROCm",
                  "maestro": "Built in for the LLM (llama-server)", "source": "docs/LLM-runtime.md", "confidence": "official"},
    "faster_whisper": {"label": "faster-whisper", "hardware": "CPU (int8) · CUDA",
                       "maestro": "Built in for transcription", "source": "app/services/audio_analysis.py",
                       "confidence": "official"},
    "python_cpu": {"label": "PyTorch on CPU", "hardware": "Any x86-64 / ARM64 CPU",
                   "maestro": "Light runtime (env-light) installs CPU PyTorch", "source": "docs/INSTALAR-SIN-NVIDIA.md",
                   "confidence": "official"},
}

# ---------------------------------------------------------------------------
# Families: licence, routes and local engines for each group of Maestro models
# ---------------------------------------------------------------------------

def _r(route: str, note: str = "", confidence: str = "", source: str = "", models: str = "") -> dict[str, str]:
    """``models`` restricts the route to matching Maestro model ids (a regex): a service that hosts the
    base model does not host every fine-tune or derivative built on it."""
    out = {"route": route, "note": note, "models": models}
    if confidence:
        out["confidence"] = confidence
    if source:
        out["source"] = source
    return out


FAMILIES: dict[str, dict[str, Any]] = {
    # ---- video ----------------------------------------------------------
    "wan": {
        "label": "Wan 2.1 / 2.2 and derivatives (VACE, Fun, Phantom, MultiTalk, InfiniteTalk, SkyReels, Ovi, Lucy, …)",
        "modality": "video", "license": "Apache-2.0 (Wan base models; derivatives keep their own card)",
        "license_url": "https://github.com/Wan-Video/Wan2.2/blob/main/LICENSE.txt", "territory": "",
        "max_output": "720p (1080p for MoviiGen); 5–10 s per window, longer with sliding windows",
        "routes": [_r("hf_zerogpu", "Community Wan 2.2 Spaces run on ZeroGPU", "secondary",
                      models=r"^(t2v_2_2|i2v_2_2|ti2v_2_2)"),
                   _r("hf_inference_providers", "Wan2.2-TI2V-5B served via fal / WaveSpeed / Replicate; $0.10/month free",
                      "secondary", models=r"^ti2v_2_2$"),
                   _r("alibaba_model_studio", "Wan 2.x hosted by Alibaba; check the free-quota tab", "secondary",
                      models=r"^(t2v|i2v|t2v_2_2|i2v_2_2|ti2v_2_2|flf2v_720p|i2v_720p)$"),
                   _r("replicate", "Wan 2.2 T2V Fast ≈ $0.05 (480p) – $0.10 (720p) per video — integrated",
                      models=r"^(t2v_2_2|ti2v_2_2)$"),
                   _r("fal", models=r"^(t2v|i2v|t2v_2_2|i2v_2_2|ti2v_2_2|vace_14B|animate)$")],
        "local": ["wan2gp_cuda", "comfyui", "sdcpp"],
    },
    "hunyuan": {
        "label": "HunyuanVideo 1.0 / 1.5 (T2V, I2V, Avatar, Custom, upsamplers)", "modality": "video",
        "license": "Tencent Hunyuan Community License",
        "license_url": "https://huggingface.co/tencent/HunyuanVideo-1.5",
        "territory": "Licence does NOT apply in the European Union, United Kingdom or South Korea",
        "max_output": "720p native; 1080p with the 1.5 upsampler",
        "routes": [_r("hf_zerogpu", "A 'Tencent HunyuanVideo 1.5' Space is listed (community)", "unverified",
                      models=r"^hunyuan_1_5"),
                   _r("fal", "HunyuanVideo 1.5 paid per second", models=r"^hunyuan_1_5")],
        "local": ["wan2gp_cuda", "comfyui", "sdcpp"],
    },
    "ltx2": {
        "label": "LTX-2.0 / LTX-2.3 (19B / 22B, dev / distilled / GGUF)", "modality": "video",
        "license": "LTX-2 Community License (free below $10M annual revenue)",
        "license_url": "https://huggingface.co/Lightricks/LTX-2", "territory": "",
        "max_output": "up to 1080p / 4K on the official API; local depends on VRAM",
        "routes": [_r("hf_zerogpu", "'LTX-2 Video [Turbo]' and 'LTX 2.3 Studio' Spaces on ZeroGPU", "secondary"),
                   _r("ltx_api"), _r("fal")],
        "local": ["wan2gp_cuda", "comfyui", "sdcpp"],
    },
    "ltx25": {
        "label": "LTX-2.5 (22B audio-video)", "modality": "video",
        "license": "LTX-2.x Community License (free below $10M revenue); Hugging Face repo is gated",
        "license_url": "https://huggingface.co/Lightricks/LTX-2.5-Diffusers", "territory": "",
        "max_output": "4K HDR claimed by the release; local depends on VRAM",
        "routes": [_r("ltx_api", "ltx-2-5-fast / ltx-2-5-pro")],
        "local": ["wan2gp_cuda", "comfyui"],
    },
    "ltxv": {
        "label": "LTX-Video 0.9.8 (13B)", "modality": "video", "license": "LTXV Open Weights License",
        "license_url": "https://huggingface.co/Lightricks/LTX-Video", "territory": "",
        "max_output": "720p+", "routes": [_r("hf_zerogpu", "Official LTX-Video demo Spaces", "unverified")],
        "local": ["wan2gp_cuda", "comfyui"],
    },
    "kandinsky5": {
        "label": "Kandinsky 5 (Lite 2B / Pro 19B)", "modality": "video", "license": "Apache-2.0 (reported)",
        "license_url": "https://huggingface.co/kandinskylab", "territory": "", "max_output": "up to 10 s",
        "routes": [_r("fal", "≈ $0.08 per 5 s (distilled $0.05)")],
        "local": ["wan2gp_cuda", "comfyui"],
    },
    "longcat": {
        "label": "LongCat-Video / Avatar (13.6B)", "modality": "video", "license": "MIT (reported, unconfirmed)",
        "license_url": "https://huggingface.co/meituan-longcat", "territory": "",
        "max_output": "long videos (minutes) by continuation", "routes": [], "local": ["wan2gp_cuda"],
    },
    "minimax_h3": {
        "label": "MiniMax H3 (Omni, First/Last, VDN, Voice Audio, community fused/turbo variants)", "modality": "video",
        "license": "MiniMax H3 licence (commercial use allowed with attribution; >$20M revenue needs authorisation)",
        "license_url": "https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/docs/QA-about-License.md",
        "territory": "Licence territory reportedly EXCLUDES the United States, European Union, United Kingdom and "
                     "South Korea for local deployment and display of outputs",
        "max_output": "768p locally (the 2K stage is API-only)",
        "routes": [_r("minimax_api", "Hosted H3 (also via OpenRouter) — available worldwide")],
        "local": ["wan2gp_cuda", "comfyui"],
    },
    # ---- image ----------------------------------------------------------
    "flux1_schnell": {
        "label": "FLUX.1 [schnell]", "modality": "image", "license": "Apache-2.0",
        "license_url": "https://huggingface.co/black-forest-labs/FLUX.1-schnell", "territory": "",
        "max_output": "≈ 2 MP",
        "routes": [_r("cloudflare", "@cf/black-forest-labs/flux-1-schnell — integrated in Maestro", "official"),
                   _r("pollinations", "FLUX images anonymously", "secondary"),
                   _r("nvidia_build", "FLUX.1 [schnell] hosted", "secondary"),
                   _r("hf_inference_providers"), _r("replicate", "≈ $0.003/image — integrated")],
        "local": ["wan2gp_cuda", "comfyui", "sdcpp"],
    },
    "flux1_dev": {
        "label": "FLUX.1 [dev] and fine-tunes (Kontext, Krea, SRPO, USO, UMO, DreamOmni2)", "modality": "image",
        "license": "FLUX.1 [dev] Non-Commercial License",
        "license_url": "https://huggingface.co/black-forest-labs/FLUX.1-dev/blob/main/LICENSE.md", "territory": "",
        "max_output": "≈ 2 MP",
        "routes": [_r("nvidia_build", "FLUX.1 [dev] / Kontext hosted", "secondary", models=r"^(flux|flux_dev_kontext)$"),
                   _r("hf_inference_providers", "FLUX.1-dev served by several providers", "secondary", models=r"^flux$"),
                   _r("hf_zerogpu", "Official BFL demo Spaces", "unverified", models=r"^(flux|flux_dev_kontext|flux_krea)$"),
                   _r("fal", models=r"^(flux|flux_dev_kontext|flux_krea)$"), _r("replicate", models=r"^(flux|flux_dev_kontext|flux_krea)$")],
        "local": ["wan2gp_cuda", "comfyui", "sdcpp"],
    },
    "chroma": {
        "label": "Chroma1 HD / Radiance (8.9B)", "modality": "image", "license": "Apache-2.0 (reported)",
        "license_url": "https://huggingface.co/lodestones/Chroma1-HD", "territory": "", "max_output": "≈ 1–2 MP",
        "routes": [], "local": ["wan2gp_cuda", "comfyui", "sdcpp"],
    },
    "flux2_dev": {
        "label": "FLUX.2 [dev] 32B and pi-FLUX.2", "modality": "image", "license": "FLUX Non-Commercial License",
        "license_url": "https://huggingface.co/black-forest-labs/FLUX.2-dev", "territory": "",
        "max_output": "up to 4 MP",
        "routes": [_r("cloudflare", "FLUX.2 [dev] listed by a third-party plugin for Workers AI", "unverified"),
                   _r("bfl_api"), _r("fal")],
        "local": ["wan2gp_cuda", "comfyui", "sdcpp"],
    },
    "flux2_klein_4b": {
        "label": "FLUX.2 [klein] 4B (and Base)", "modality": "image", "license": "Apache-2.0",
        "license_url": "https://huggingface.co/black-forest-labs/FLUX.2-klein-4B", "territory": "",
        "max_output": "up to 4 MP",
        "routes": [_r("cloudflare", "@cf/black-forest-labs/flux-2-klein-4b (4 steps) — integrated in Maestro", "official",
                      "https://developers.cloudflare.com/workers-ai/models/flux-2-klein-4b/"),
                   _r("vercel_ai_gateway"), _r("nvidia_build", "FLUX.2 Klein hosted", "secondary"), _r("bfl_api")],
        "local": ["wan2gp_cuda", "comfyui", "sdcpp"],
    },
    "flux2_klein_9b": {
        "label": "FLUX.2 [klein] 9B (and Base)", "modality": "image", "license": "FLUX Non-Commercial License",
        "license_url": "https://huggingface.co/black-forest-labs/FLUX.2-klein-9B", "territory": "",
        "max_output": "up to 4 MP; up to 4 reference images",
        "routes": [_r("cloudflare", "@cf/black-forest-labs/flux-2-klein-9b — integrated in Maestro", "official",
                      "https://developers.cloudflare.com/workers-ai/models/flux-2-klein-9b/"),
                   _r("vercel_ai_gateway"), _r("bfl_api")],
        "local": ["wan2gp_cuda", "comfyui", "sdcpp"],
    },
    "qwen_image": {
        "label": "Qwen-Image 20B (2512, Edit, Edit Plus, Layered, Lightning, GGUF, Nunchaku)", "modality": "image",
        "license": "Apache-2.0", "license_url": "https://huggingface.co/Qwen/Qwen-Image", "territory": "",
        "max_output": "≈ 1.7 MP (1328²)",
        "routes": [_r("modelscope", "Qwen/Qwen-Image on the free API-Inference quota — integrated in Maestro", "secondary"),
                   _r("alibaba_model_studio", "Hosted Qwen-Image APIs"), _r("fal"),
                   _r("replicate", "Qwen-Image via WaveSpeed on Replicate", "secondary")],
        "local": ["wan2gp_cuda", "comfyui", "sdcpp"],
    },
    "qwen_image_21": {
        "label": "Qwen-Image 2.1 (7B)", "modality": "image",
        "license": "Qwen Research License — non-commercial research/evaluation only",
        "license_url": "https://huggingface.co/Qwen/Qwen-Image-2.1", "territory": "", "max_output": "≈ 2 MP",
        "routes": [_r("modelscope", "Qwen/Qwen-Image-2.1 — integrated in Maestro; real-name-verified Alibaba Cloud account required",
                      "secondary"),
                   _r("alibaba_model_studio", "Qwen-Image-2.1 Pro / Turbo hosted (paid)", "secondary")],
        "local": ["wan2gp_cuda", "comfyui"],
    },
    "z_image": {
        "label": "Z-Image (Turbo / Base / ControlNet / TwinFlow, 6B)", "modality": "image", "license": "Apache-2.0",
        "license_url": "https://huggingface.co/Tongyi-MAI/Z-Image-Turbo", "territory": "", "max_output": "≈ 2 MP",
        "routes": [_r("hf_zerogpu", "Official Space Tongyi-MAI/Z-Image-Turbo", "secondary"),
                   _r("modelscope", "Tongyi-MAI/Z-Image-Turbo — integrated in Maestro", "secondary")],
        "local": ["wan2gp_cuda", "comfyui"],
    },
    "hidream_o1": {
        "label": "HiDream O1 Image (10B)", "modality": "image", "license": "See model card (not confirmed)",
        "license_url": "https://huggingface.co/HiDream-ai", "territory": "", "max_output": "≈ 1–2 MP",
        "routes": [_r("fal", "≈ $0.01 per 1024² image")], "local": ["wan2gp_cuda"],
    },
    "krea2": {
        "label": "Krea 2 (RAW / Turbo / Identity Edit, 12B)", "modality": "image",
        "license": "Krea Community License (free under 50 seats / revenue threshold; content filtering required)",
        "license_url": "https://huggingface.co/krea", "territory": "", "max_output": "≈ 2–4 MP",
        "routes": [], "local": ["wan2gp_cuda"],
    },
    # ---- audio ----------------------------------------------------------
    "ace_step": {
        "label": "ACE-Step v1 / v1.5 / XL (music)", "modality": "audio", "license": "Apache-2.0",
        "license_url": "https://github.com/ACE-Step/ACE-Step-1.5", "territory": "", "max_output": "10 s – 10 min songs",
        "routes": [_r("hf_zerogpu", "Official Space ACE-Step/Ace-Step-v1.5", "official"), _r("acemusic")],
        "local": ["wan2gp_cuda", "comfyui", "python_cpu"],
    },
    "chatterbox": {
        "label": "Chatterbox Multilingual (TTS, 23 languages)", "modality": "audio", "license": "MIT",
        "license_url": "https://huggingface.co/ResembleAI/chatterbox", "territory": "", "max_output": "speech",
        "routes": [_r("nvidia_build", "Chatterbox Multilingual model card on build.nvidia.com", "official",
                      "https://build.nvidia.com/resembleai/chatterbox-multilingual-tts/modelcard"),
                   _r("hf_zerogpu", "Resemble's English demo Space", "secondary")],
        "local": ["wan2gp_cuda", "python_cpu"],
    },
    "index_tts2": {
        "label": "IndexTTS 2 (emotional TTS / cloning)", "modality": "audio",
        "license": "Bilibili Index model licence (commercial use below user/revenue thresholds — check the file)",
        "license_url": "https://huggingface.co/IndexTeam/IndexTTS-2", "territory": "", "max_output": "speech",
        "routes": [_r("hf_zerogpu", "Official Space IndexTeam/IndexTTS-2-Demo", "official")],
        "local": ["wan2gp_cuda", "python_cpu"],
    },
    "qwen3_tts": {
        "label": "Qwen3-TTS 1.7B (Base / CustomVoice / VoiceDesign)", "modality": "audio", "license": "Apache-2.0",
        "license_url": "https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base", "territory": "", "max_output": "speech",
        "routes": [_r("alibaba_model_studio", "qwen3-tts-flash hosted ($0.115 / 10k chars; free quota only in console)",
                      "official", "https://www.alibabacloud.com/help/en/model-studio/qwen3-tts-flash")],
        "local": ["wan2gp_cuda", "python_cpu"],
    },
    "yue": {
        "label": "YuE / YuE2 (songs with vocals)", "modality": "audio", "license": "See model card (non-commercial terms)",
        "license_url": "https://huggingface.co/m-a-p/YuE2-3B", "territory": "", "max_output": "full songs",
        "routes": [], "local": ["wan2gp_cuda"],
    },
    "heartmula": {
        "label": "HeartMuLa OSS 3B (music)", "modality": "audio", "license": "See model card",
        "license_url": "https://huggingface.co/HeartMuLa", "territory": "", "max_output": "songs",
        "routes": [], "local": ["wan2gp_cuda"],
    },
    "kugelaudio": {
        "label": "KugelAudio 0 Open 7B (TTS)", "modality": "audio", "license": "See model card",
        "license_url": "https://huggingface.co/kugelaudio/kugelaudio-0-open", "territory": "", "max_output": "speech",
        "routes": [], "local": ["wan2gp_cuda"],
    },
    "minimax_music3": {
        "label": "MiniMax-Music3", "modality": "audio", "license": "See model card",
        "license_url": "https://huggingface.co/MiniMaxAI/MiniMax-Music3", "territory": "Check the card's territory clause",
        "max_output": "songs", "routes": [_r("minimax_api", "MiniMax music API (paid)", "unverified")],
        "local": ["wan2gp_cuda"],
    },
    "ltx_audio": {
        "label": "LTX audio models (Scenema Audio, DramaBox)", "modality": "audio",
        "license": "LTX-2 Community License", "license_url": "https://huggingface.co/Lightricks/LTX-2",
        "territory": "", "max_output": "audio", "routes": [], "local": ["wan2gp_cuda"],
    },
    "mmaudio": {
        "label": "MMAudio (sound effects for video)", "modality": "audio", "license": "MIT (code) / CC-BY-NC (weights)",
        "license_url": "https://huggingface.co/hkchengrex/MMAudio", "territory": "", "max_output": "audio",
        "routes": [_r("hf_zerogpu", "Official demo Space hkchengrex/MMAudio", "unverified")],
        "local": ["wan2gp_cuda", "python_cpu"],
    },
    "whisper": {
        "label": "Whisper (transcription)", "modality": "text", "license": "MIT",
        "license_url": "https://huggingface.co/Systran/faster-whisper-small", "territory": "", "max_output": "text",
        "routes": [_r("cloudflare", "@cf/openai/whisper-large-v3-turbo ≈ 47 Neurons/min", "official"),
                   _r("groq", "whisper-large-v3 / -turbo")],
        "local": ["faster_whisper"],
    },
    # ---- text -----------------------------------------------------------
    "gemma4": {
        "label": "Gemma 4 (Maestro ships uncensored/abliterated GGUF fine-tunes)", "modality": "text",
        "license": "Gemma terms of use (base); fine-tunes add their own card",
        "license_url": "https://ai.google.dev/gemma/terms", "territory": "", "max_output": "text",
        "routes": [_r("google_ai_studio", "Base Gemma 4 (not the fine-tunes Maestro ships)", "unverified"),
                   _r("openrouter_free", "Gemma variants appear and disappear from the free list", "unverified")],
        "local": ["llama_cpp"],
    },
    "qwen_llm": {
        "label": "Qwen 3.5 / 3.6 / 3.8 (GGUF fine-tunes)", "modality": "text", "license": "Apache-2.0 (base)",
        "license_url": "https://huggingface.co/Qwen", "territory": "", "max_output": "text",
        "routes": [_r("openrouter_free", "Qwen variants rotate on the free list", "unverified"),
                   _r("siliconflow", "Small base Qwen models at $0 — not the fine-tunes Maestro ships", "secondary",
                      models="base_only")],
        "local": ["llama_cpp"],
    },
}

# model_type → family.  Order matters: first match wins.
_RULES: list[tuple[str, str]] = [
    (r"^(minimax_h3|viggle_animate)", "minimax_h3"),
    (r"^ltx2_25", "ltx25"), (r"^(ltx2|ltx2_)", "ltx2"), (r"^ltxv", "ltxv"),
    (r"^(scenema_audio|dramabox_audio)", "ltx_audio"),
    (r"^hunyuan", "hunyuan"), (r"^k5_", "kandinsky5"), (r"^longcat", "longcat"),
    (r"^flux_schnell", "flux1_schnell"), (r"^flux_chroma", "chroma"),
    (r"^(flux2_dev|pi_flux2)", "flux2_dev"), (r"^flux2_klein(_base)?_4b", "flux2_klein_4b"),
    (r"^flux2_klein(_base)?_9b", "flux2_klein_9b"), (r"^flux", "flux1_dev"),
    (r"^qwen_image_21", "qwen_image_21"), (r"^qwen_image", "qwen_image"), (r"^z_image", "z_image"),
    (r"^hidream", "hidream_o1"), (r"^krea2", "krea2"),
    (r"^ace_step", "ace_step"), (r"^chatterbox", "chatterbox"), (r"^index_tts", "index_tts2"),
    (r"^qwen3_tts", "qwen3_tts"), (r"^yue", "yue"), (r"^heartmula", "heartmula"), (r"^kugelaudio", "kugelaudio"),
    (r"^minimax_music", "minimax_music3"),
    (r".*", "wan"),
]
_LLM_RULES = [(r"gemma", "gemma4"), (r"qwen", "qwen_llm")]


def family_for(item_id: str, kind: str = "model") -> str:
    if kind == "llm":
        lowered = item_id.lower()
        return next((family for pattern, family in _LLM_RULES if pattern in lowered), "qwen_llm")
    if kind == "extra":
        return {"mmaudio": "mmaudio", "whisper_small": "whisper"}.get(item_id, "")
    return next(family for pattern, family in _RULES if re.match(pattern, item_id))


def access_for(family: str, item_id: str = "") -> dict[str, Any]:
    """The family's licence plus each route (that applies to ``item_id``) expanded with the service's facts."""
    data = FAMILIES.get(family)
    if not data:
        return {}
    routes = []
    for entry in data["routes"]:
        same_weights = entry.get("models") != "base_only"
        if same_weights and entry.get("models") and item_id and not re.search(entry["models"], item_id):
            continue
        service = ROUTES[entry["route"]]
        routes.append({
            "id": entry["route"], "label": service["label"], "kind": service["kind"],
            "kind_label": ROUTE_KINDS[service["kind"]], "note": entry.get("note", ""),
            "limits": service["limits"], "account": service["account"], "maestro": service["maestro"],
            "source": entry.get("source") or service["source"],
            "confidence": entry.get("confidence") or service["confidence"],
            "same_weights": same_weights,
        })
    if "wan2gp_cuda" in data["local"]:
        # Every model Maestro's NVIDIA engine runs can also run on a free NVIDIA notebook GPU,
        # VRAM permitting (16 GB on a T4).
        for key in ("kaggle_notebook", "lightning_studio"):
            service = ROUTES[key]
            routes.append({"id": key, "label": service["label"], "kind": service["kind"],
                           "kind_label": ROUTE_KINDS[service["kind"]],
                           "note": "Same NVIDIA hardware class Maestro targets; fits if the model fits in 16 GB VRAM",
                           "limits": service["limits"], "account": service["account"], "maestro": service["maestro"],
                           "source": service["source"], "confidence": service["confidence"], "same_weights": True})
    local = [{"id": key, **LOCAL_BACKENDS[key]} for key in data["local"]]
    return {**{k: v for k, v in data.items() if k not in ("routes", "local")}, "family": family,
            "routes": routes, "local_backends": local,
            "free_api": any(r["kind"] == "free_recurring" and r["confidence"] != "unverified" and r["same_weights"]
                            for r in routes),
            "researched_on": RESEARCHED_ON}


_PARAMS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*B\b")


def local_feasibility(name: str, modality: str, hardware: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """An honest, coarse expectation for running a model without an NVIDIA GPU.

    It is an estimate from the parameter count in the model's name and its modality,
    labelled as such; nothing here is a measurement.
    """
    match = _PARAMS_RE.search(name or "")
    params = float(match.group(1)) if match else None
    ram_gb = round(params * 2 * 1.3, 1) if params else None  # bf16 weights + working memory
    if modality == "video":
        cpu = "impractical"
        why = "Video diffusion on a CPU takes hours per clip; use a supported AMD/Apple GPU, a peer or an API."
    elif modality == "image":
        if params is not None and params <= 6:
            cpu, why = "slow", "Minutes per image on a modern CPU; GGUF in stable-diffusion.cpp is the lightest path."
        else:
            cpu, why = "very_slow", "Tens of minutes per image on a CPU and ≥ 32 GB RAM; prefer GGUF or an API."
    elif modality == "audio":
        cpu = "slow" if (params or 2) <= 4 else "very_slow"
        why = "Small TTS/music models run on CPU at below real-time speed."
    else:
        cpu, why = "practical", "GGUF language models and faster-whisper run on any CPU."
    return {"params_b": params, "estimated_ram_gb": ram_gb, "cpu": cpu, "why": why, "basis": "estimate"}
