---
name: hfsy-api
description: Invoke HFSY API models from Codex. Activate when the user mentions @https://www.hfsyapi.cn/, asks to initialize HFSY API, or requests a model from the HFSY model square.
---

# HFSY API

Use the bundled MCP `hfsy` tool for every catalog, upload, call, and query action. Do not run `hfsy.py`, shell commands, `print`, or any local fallback for an HFSY request. A generation request is not submitted until the `hfsy` MCP tool call is visible. The server requires Python 3, with no third-party packages. The key and catalog remain in the running server process after initialization; they are not guaranteed across new conversations or app restarts.

The plugin exposes an MCP **tool**, not an MCP resource. An empty `List MCP resources` result is expected and does not show that `hfsy` is unavailable. Discover the `hfsy` tool through the available MCP tools or tool search, then call it directly. If tool discovery truly does not expose `hfsy`, explain that the plugin's MCP server was not loaded in this thread. Check that the plugin is installed and enabled, Python 3 is available to Codex, and start a new thread after installation. Do not ask for or save an API key until the tool is available; do not claim the key is invalid on the basis of an empty resource list.

## Initialization

When the user sends `@https://www.hfsyapi.cn/` or asks to initialize HFSY API, ask for the API Key if one is not already present in the current conversation. Send the key only in MCP tool arguments. These may remain in Codex tool history; this is not a password-masked input dialog. Never put it in a file, shell argument, environment variable, log, or response. A newly supplied key replaces the previous session key.

After initialization, call the catalog action with the key. Explain that the key is retained only for the current Codex conversation and that the user can replace it by sending a new initialization message.

## Material uploads

For a generation request, call `hfsy` with `action: "call"` once and include any
local image, video, and audio paths in `files`. Do not call `upload` separately.
The task internally uploads supplied materials, then submits their URLs to the
model. With no materials, omit `files` or pass `[]`: submit directly without
calling any upload endpoint. Images use `/v1/files/image-upload`; videos and
audio use `/v1/files/video-upload` and `/v1/files/audio-upload` with multipart
field `file`. All use the session API key. Video generation groups the resulting
URLs into `images`, `videos`, and `audios`.

Treat uploads as internal steps. Do not display input media URLs, upload responses,
or separate upload-success messages. Poll asynchronous generation until a final
result or a bounded timeout; task submission is not the final generated result.
Present the final result once. On failure, report the error without resubmitting.

The standalone `upload` action remains available only for an explicit upload-only
request. It accepts exactly one local path in `files` and an optional key; omit
model, prompt, params, dry_run, and task fields. Only show its URL when the user
explicitly requests the uploaded address. Never use it as a precursor to `call`.

## Model calls

Initialization and explicit `catalog` refreshes read `/api/docs` alongside `/api/pricing` and `/v1/models`. The API documentation is matched by exact model name. Recognized request-parameter descriptions supply missing limits; when both sources publish a limit, validation enforces both. Inspect `raw.documentation.requestParams` and `errorTips` for additional restrictions. Reference media duration, dimensions and codecs are not automatically measured by the plugin; do not claim all documentation requirements were validated. If a limit remains unrecognized, report that the parser could not find it in the catalog or documentation, rather than asserting that the website does not publish it. Do not bypass validation or rewrite the user's prompt.

The user's prompt is an opaque payload. Copy it byte-for-byte into the `prompt` argument: preserve every character, space, line break, punctuation mark, and language. Never add an opening such as "生成一张", visual details, style words, negative prompts, translations, summaries, safety wording, or any other text. Never remove or normalize text. Only put separately specified options in `params`; do not encode options into the prompt. The platform's `revised_prompt` is a provider response and must not be sent back as a new prompt.

The catalog is refreshed during initialization. For later calls in the same server process, use the cached catalog to validate the requested model and parameters. Use the `call` action with the model name, prompt, optional local image, video, and audio files, and parameters. For videos, poll the returned task ID with the `query` action until completion, failure, or a bounded timeout. Never submit a second generation request after a timeout or display failure.

Supported parameter names include `aspect_ratio`, `image_output_resolution`, `video_duration`, and `video_orientation`. Use platform enum values from the refreshed catalog, such as `portrait` for vertical video.

Translate errors clearly, including invalid key, unavailable model, insufficient balance, timeout, invalid parameters, and network errors. Validation failures must stop before a paid request.

## Results

After a successful image generation or image query, immediately display the returned image in the final response. Prefer an artifact path in `result.artifacts`: embed its absolute path using Markdown image syntax (`![生成图片](absolute-path)`) so the image appears inline in chat. Normalize Windows separators to `/` in the Markdown target when needed. Do not describe the image without embedding it when a local image artifact is available. Show only the final image; do not add duplicate result links unless requested. Forward native MCP image content when returned; if orchestrating through functions.exec, forward every image block with `image(block)`, not `text(block)`. Never print Base64 into the final response. Native image-block rendering depends on the Codex client, so still embed the local artifact path when available. If no local artifact exists, include the provided `markdown_image` and `markdown_link` for each remote image URL. Do not regenerate to fix a display failure.

For successful video results, show both the video file and its original URL in the final response. Use each video artifact's `markdown_file` as the clickable file entry (or build `[生成的视频.mp4](<absolute-path>)` from its path, using forward slashes). Below the file, write `视频 URL：` and render the matching `media_urls[].result_url_code_block` verbatim as a fenced plain-text code block. It contains only the original URL so the client's code-block copy control copies just the URL. If that field is missing (for example, an older server is still running), put `media_urls[].url` in a fenced `text` code block yourself. Do not render the remote video URL as a Markdown link, autolink, bare clickable URL, or separate playback link. If no local artifact is returned, still show the URL code block. The client controls copy-button availability and position; do not claim a custom inline button was installed or verified. Preserve native MCP video resources and local file resource links when the tool interface supports forwarding them. Never serialize resource blobs as text or print video Base64 while forwarding tool results. Do not claim that a file attachment card or playback has appeared without observing it. Use artifacts already returned by the plugin; do not download remote media to work around preview restrictions or regenerate for a display problem.

Do not expose API keys or opaque provider signatures. Keep user-facing replies concise and in the user's language.
