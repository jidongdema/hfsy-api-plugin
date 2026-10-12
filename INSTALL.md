# Install HFSY API for Codex

This repository is a Codex marketplace. Keep `.agents/plugins/marketplace.json`
and `plugins/hfsy-api/` together when cloning or downloading it.
Do not install only the `skills/` directory: that omits the MCP server.

Prerequisites: Codex desktop with plugin support and Python 3 on the PATH seen
by Codex. On Windows, verify `python --version`; on macOS and Linux, verify
`python3 --version`.

From a terminal, run:

```sh
codex plugin marketplace add https://github.com/jidongdema/hfsy-api-plugin.git --ref main
codex plugin add hfsy-api@hfsy-local
codex plugin list
```

The list must show `hfsy-api@hfsy-local` as `installed, enabled`. Start a new
Codex conversation after installation so its MCP tools are loaded. Then ask to
initialize HFSY API. The plugin offers the MCP **tool** `hfsy`; it offers no MCP
resources, so `List MCP resources` can be empty even when installation works.

If `hfsy` is absent from the available tools in a new conversation, first check
the installed/enabled status, then confirm Python 3 is available to the Codex
app. Do not send an API key until the tool is available.
