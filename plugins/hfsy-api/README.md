# HFSY API MCP plugin

This plugin includes a dependency-free Python 3 stdio MCP adapter. The `hfsy`
tool accepts catalog, upload, call, and query actions. Initialize with a key once per running conversation; later calls reuse it.
Keys are never persisted by the server, but may remain in Codex tool history.
No password dialog or cross-conversation credential storage is provided.

Provider-returned Base64 images and hosted image URLs are emitted as MCP image
content with detected MIME types. Hosted video URLs are downloaded as MP4
artifacts and returned as standard MCP embedded resources with `video/mp4`; a
resource link is also retained as a fallback. Video embedding is limited to 8 MB
before Base64 encoding; image downloads are limited to 25 MB. Files are saved
under `scripts/outputs`. Larger videos retain their original result URL.
The MCP block type is `resource`, not `embedded_resource`. Protocol correctness
does not establish that Codex renders the resource as an inline video player;
client playback remains unverified. Videos do not receive Markdown image syntax.
Successful video artifacts also include `name`, `source_url`, and `markdown_file`,
and the MCP response includes a local video resource link with a filename and MIME
type. The final reply shows the clickable local video file followed by the original
URL as a plain-text fenced code block (`result_url_code_block`); `result_url_text`
also holds the raw URL. The code block lets supporting clients offer their native
copy button without turning the URL into a playback link. Button availability and
position belong to the client, not this stdio plugin. Remote video resource links
are not emitted. Oversized or unavailable downloads still return the URL code block.
Image link fields are unchanged. File-card appearance and preview support depend
on the client.

After updating, reinstall `hfsy-api@hfsy-local` and start a new task to load the
MCP tool. The plugin server timeout is 4800 seconds; image HTTP requests retain
their 300-second timeout. No generation request is automatically retried.

## Conversation workflow

For generation, send a single `call` with optional local image/video/audio paths
in `files`. The task uploads only supplied materials and submits once; omitted or
empty `files` bypasses uploads. Intermediate upload URLs and responses are not
shown to the user. Poll async tasks and present only the final generated result.
Do not perform a standalone upload before a generation call.

Standalone upload uses `{"action":"upload","files":["D:/Desktop/video4.mp4"]}`.
It accepts exactly one local image, video, or audio file and an optional `key`,
reusing the session key otherwise. No model or prompt is required and no model
catalog is fetched for upload. The result contains `url`, `kind`, `mime`, `name`,
and `size`, plus the provider JSON in `response`. Standalone upload does not emit a visible resource-link card.
Video and audio uploads use `/v1/files/video-upload` and `/v1/files/audio-upload`
with `Authorization: Bearer <API key>`; no website session cookie is needed.
The result is returned directly without downloading or submitting generation.
Uploads are not automatically retried. Reload the updated plugin in a new chat
to expose the new action; an already-running server keeps its old tool schema.

The user supplies a key once in the current conversation. Codex sends structured
JSON on stdin to the caller, reusing the current conversation's key, and translates
structured errors back into the user's language. Never place the key in command
arguments, repository files, environment files, logs, or final replies. A new key
replaces the old key; do not fall back to the old key if validation fails.

This helper keeps the key and authorized model catalog in the running MCP process
after initialization; later calls in the same conversation can omit the key.
Conversation retention is controlled by Codex, so a key pasted into chat
may remain in chat/tool history. Context truncation, application restarts, and
process termination cannot be promised to preserve memory-only credentials.

Input commands (stdin JSON, shown without a real key):

```json
{"action":"catalog"}
{"action":"catalog","key":"USER_SUPPLIED_KEY"}
{"action":"call","key":"USER_SUPPLIED_KEY","model":"nano-banana-pro","prompt":"A red bicycle","files":[],"params":{"aspect_ratio":"1:1","image_output_resolution":"2K"},"dry_run":true}
{"action":"query","task_type":"image","task_id":"RETURNED_ID"}
```

`dry_run` defaults to false. It refreshes and validates without generating when true.
Network permissions remain subject to the Codex host. A key alone cannot grant
network permission. No background process is required. All requests must use the
bundled `hfsy` MCP tool; do not substitute a terminal command or local script.

## Confirmed platform behavior (2026-09-20)

- `https://www.hfsyapi.cn/api/pricing` returned 33 models, categories, endpoint
  types, and optional `constraints` JSON strings. Live documentation parsing is described below.
- `/v1/models` is used for key authentication and model visibility; authenticated
  behavior has not been verified because no API key has been supplied.
- Marketplace configuration, API documentation and key visibility refresh once when a key is initialized;
  call `catalog` explicitly to refresh it.
  Failure stops generation, with no stale fallback or automatic paid retry.
- Platform constraints include prompt characters, image size, total image bytes,
  reference counts, aspect ratios, duration, orientation, and output resolution.
- The site's global `openai` endpoint maps to `/v1/video/create`, even for text
  model entries. Adapters therefore select protocols by category and endpoint type.
- Public frontend requests confirm chat, image generation, Gemini, Midjourney,
  video creation, image async submission, and image/video/Midjourney query routes used by this implementation.

## Current limits, not completion claims

Release 0.1.3 reads the same `/api/docs` JSON endpoint used by the website's
API documentation page, concurrently with `/api/pricing` and `/v1/models` on
initialization or explicit catalog refresh. Documentation is matched by exact
model name; it is never copied from a similarly named model. No hard-coded
per-model size fallback remains. A failed refresh clears the old catalog.

Recognized `requestParams` descriptions provide prompt character limits, per-file
bytes, image total bytes, reference counts, output duration options, aspect ratios,
orientations and image resolutions. Catalog fields remain visible as the primary
values, but validation also enforces documented limits when they disagree. Missing
or unrecognized required limits still stop submission; the error no longer claims
that the website necessarily omitted them. Examples and document text are data,
never executed commands or instructions. The document endpoint receives no key.

Each catalog row preserves `raw.documentation` with the source URL, parameter
table, error tips and validation scope. This is a bounded parser, not an arbitrary
natural-language interpreter. Reference clip duration, total media duration,
resolution, codecs and combination rules are not automatically validated. Review
the retained documentation for these requirements before generation. Passing
preflight does not guarantee provider acceptance.

Verified on 2026-10-09 against
https://www.hfsyapi.cn/docs/api?model=sd-2.5-480: 4–30 second output, up to 30
images / 10 videos / 10 audio files; 20 MB per image (recommended by the docs,
required by the catalog), 30 MB total image bytes, 200 MB per video, 15 MB per
audio. MB uses the existing binary-MB convention. The docs additionally specify
3–10 seconds per reference video/audio, at most 15 seconds total per type, and
720–2160 px for reference videos; these require separate media inspection.

Install the updated source and restart Codex with a new conversation to load the
new server. An existing MCP process retains its already-imported Python code.

The public catalog omits text context/token limits and many prompt/file limits.
Unknown is not unlimited. Calls with missing required validation limits stop with
`unpublished_limit`; therefore current text entries cannot yet pass strict preflight.
Accurate token checks additionally require platform tokenizer and message accounting
information. Context window tokens and prompt character limits are different units.

Local image, video, and audio files are uploaded before generation. Images use
`/v1/files/image-upload` with a JSON body containing `base64`, `mime_type`, and
`filename`; videos and audio use `/v1/files/video-upload` and
`/v1/files/audio-upload`, respectively, with multipart form field `file`. The
returned `data.url` is passed to the model request.
For `/v1/video/create`, uploaded references are grouped into the `images`,
`videos`, and `audios` URL arrays. In particular, local videos are sent as multipart
`file` uploads to `https://www.hfsyapi.cn/v1/files/video-upload`, then
the returned `data.url` is included in `videos` (never the local file path).
Extension-based MIME detection
is not a substitute for a complete media decoder, and unfamiliar protocols still
fail explicitly before generation.

Image/video generation adapters and key authorization still need real-key integration
testing. New model names and changed values for supported constraints need no code
change. New protocol formats or constraint semantics need an adapter, or a server
published machine-readable schema. Arbitrary new semantics cannot be inferred safely.

Video and Midjourney async submissions return a platform task ID. Codex should
query that ID, display intermediate status, and stop on completion/failure or a
bounded timeout; never submit again just because polling timed out. Image models
use the catalog-declared `/v1/images/generations` upstream route; the frontend-only
`/pg/images/async-generations` path is not used because it returns HTML on the API host.
Returned text can be quoted in chat;
URLs should be displayed through supported media rendering, and base64 media must
be decoded into local artifacts before display. The caller extracts API
`inlineData` / `inline_data` / `b64_json` images and downloaded hosted image URLs
into persistent plugin output files, returns native MCP image content, and removes
opaque thought signatures from terminal output.

Image generation endpoints wait up to 300 seconds and video generation endpoints
wait up to 4800 seconds. This avoids discarding slow successful generations after
the previous 60-second client deadline; it never retries a timed-out generation
automatically.

After every successful image result, Codex should immediately embed the returned
local artifact path with Markdown image syntax in the final chat response, without duplicating the result with an extra original-image link unless requested. For URL-only results, `media_urls`
contains both a Markdown image and a clickable original-image link; use a single final presentation, preferring the image.
Never regenerate to fix display errors.
Local artifacts remain usable on this computer while the files exist. Sharing a
conversation does not copy these files to another user's computer. External URL
availability and Codex rendering cannot be guaranteed forever.

Run offline checks from the marketplace root with
`python -m unittest discover -s plugins/hfsy-api/tests -v`. These validate refresh, rejection,
key replacement, payload mapping and error handling; they are not live generation
tests. The remaining integration requires a key and the missing upload/limit schema.
