# Free public compute: rendering on nobody's computer

Maestro is local-first. That order never changes, and this document is about the
last tier of it:

```
this computer (CUDA here)
    → a peer Maestro you own        docs/STUDIO-NODES.md
    → public compute (this)         a free public GPU on the internet
```

The third tier exists for one situation: **a computer with no GPU and no second
machine**. A modest laptop, a Mac, a machine whose card is not NVIDIA. Before it,
such a computer could direct, plan, edit and assemble but not render; now it
renders, on a free public engine that Maestro connects to on its own.

## What it actually does

Nothing has to be installed, configured, paid for or signed into. On startup — and
again on a timer, and again on the spot before a request would otherwise be refused
— Maestro:

1. **Asks the public Hugging Face Hub** which Spaces are `RUNNING` right now on
   free ZeroGPU hardware (`zero-…`) for the capability the request needs:
   `text-to-image`, `text-to-video`, `image-to-video`, `text-to-speech`,
   `text-to-audio`. The SDK and the runtime stage are asked for explicitly: a plain
   listing does not include them, and without them "is this Space running on a free
   GPU now" could only be guessed. A paused, erroring, CPU-only or non-Gradio Space
   is never a candidate.
2. **Reads each candidate's own API** (`GET https://<space>.hf.space/gradio_api/info`)
   and picks the endpoint that really renders. The Space's report decides, not its
   name:
   - an endpoint *called* like the capability wins (`/text_to_video`);
   - Gradio's own event callbacks (`/_on_user_loras_change`, `/lambda_2`) are
     excluded — they take a string too, and a render sent to one of them would
     report success while producing nothing usable;
   - an endpoint that returns text (a prompt builder, a prompt enhancer) is not a
     renderer, so its declared return type is checked;
   - an endpoint that *demands* an input file is not usable for a text request, and
     a capability whose request brings a start frame needs an endpoint that can
     receive it.
3. **Connects to the best of what answered** — an endpoint named after the
   capability first, then the most liked Space — keeps a couple of further Spaces
   as alternates, and remembers the connection (with its endpoint and its declared
   parameters) under `app/settings/public_cloud/public_cloud.json`. There is no
   secret in that file: public Spaces are anonymous, so the record is only the
   measurement, and it is re-measured on a schedule.

A Space that disappears next month is re-discovered, not worked around. A Space
that goes down overnight stops being a source after its probe goes stale.

## What the render is, and what it is not

A public Space runs its own model with its own defaults. Maestro maps the request
onto what the Space declared — the prompt, a negative prompt, a seed, steps, a
width/height, a duration **derived from frames and frame rate** (a frame count is
never sent as seconds: that would ask a public engine for a two-minute video), and
a start frame uploaded to a file input the Space reports.

That means:

- the route records `public_fidelity = "public_approximation"`, and the job's own
  message says the render approximates the Maestro recipe;
- a value outside the Space's declared range is brought inside it **and a note says
  so** (`steps: 4 is outside what the public engine supports (10-60), so 10 was
  used`) — the engine's limit is respected without hiding the change;
- a required input nothing can fill stops the render, naming it. Sending a guess
  would produce a video that has nothing to do with what was asked for.

**A peer is always preferred over public compute**, because a peer runs this same
pipeline. Public compute is only chosen when this computer cannot generate and no
peer can either.

## When a public engine is not there

The tier is built so that somebody else's server breaking never becomes the user's
lost render:

| What happened | What Maestro does |
| --- | --- |
| The first Space returns an error, or breaks mid-render | Tries the next connected Space, and says so in the job's progress |
| The free quota is spent (`You have exceeded your ZeroGPU quota`) | **Defers** the job: it stays queued, consumes no attempt, and is retried on its own |
| The service refuses right now (429/503) | **Defers**, with the service's own message |
| No Space is connected for the capability | **Defers** — "Maestro keeps looking for one by itself" |
| The result is JSON, HTML or empty | Fails with that reason; never writes a file |
| The result is an image where a video was asked for | Fails; nothing is recorded as a render |
| No public engine at all, and no peer | Refused (409) with a reason that names both tiers |

## Free, and how to get more of it

Anonymous free ZeroGPU time is shared and small (minutes per day), with a low queue
priority. Three things raise it, all free, none of them required:

- **Sign Hugging Face in** with the launcher's optional
  **Connect Hugging Face (Optional)** action. That is the account's own free
  allowance, used through the same token the downloader already has; no key of
  Maestro's is involved, and the token never leaves the computer.
- **Wait.** A spent quota resets, and a deferred job is retried by itself.
- **Point Maestro at one particular Space** (Studio → Compute & transfer → Public
  compute → *Use one particular public Space*), which is still probed before it is
  used, so a Space that cannot run the task is reported instead of receiving work.

## The API

```bash
curl http://127.0.0.1:7860/api/v1/engines/public/providers
curl -X POST http://127.0.0.1:7860/api/v1/engines/public/connect
curl -X POST http://127.0.0.1:7860/api/v1/engines/public/spaces \
     -H 'Content-Type: application/json' \
     -d '{"space": "owner/space", "capability": "text_to_video"}'
curl -X POST http://127.0.0.1:7860/api/v1/engines/public/providers/hf-space:owner/space/probe
curl -X DELETE http://127.0.0.1:7860/api/v1/engines/public/providers/hf-space:owner/space
```

`GET …/providers` reports what is connected, what each provider declared, what the
Hub search found per capability, and the policy in words (protocol, discovery,
quota, fidelity). None of these endpoints is needed for the feature to work: the
connection happens by itself.

## What is verified, and what is not

- **Verified against the live service:** the whole path — discovery on the Hub, a
  Space's own `/info`, the Gradio call, the event stream, the file download — and a
  real image generated on a free public Space into a workspace, with a broken Space
  falling through to the next one.
- **Verified without the network** (`tests/test_public_cloud.py`, 37 tests): every
  rule above, driven through an injected HTTP transport — discovery filters,
  endpoint choice, the request mapping (including the frame-count trap and the
  range note), the fallback, the deferrals, and the refusal to record a
  non-media body as a render.
- **Not verified:** a real end-to-end *video* render. The anonymous free ZeroGPU
  quota was spent during development, and the service's own message in that case is
  exactly what the code defers on. The video path (a video result, its validation
  and its file extension) is covered by the stub tests, not by a live render. Free
  public GPU time is shared, so a video render may also queue for minutes.
