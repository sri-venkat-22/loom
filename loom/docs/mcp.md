# MCP servers

The [Model Context Protocol](https://modelcontextprotocol.io) lets programs offer tools
to AI apps. Loom can connect to MCP servers and give their tools to the agent, next to
its own: a GitHub server lets it read issues and open pull requests, a database server
lets it query your schema, a browser server lets it check a page.

```
> what's the latest issue about the parser?
● github - search_issues (MCP)(repo: "me/parser", query: "parser")
Use the github MCP tool search_issues? (Y)es/(N)o/(A)lways: always allow this tool (saved to .loom.permissions.json) [Yes]:
  ⎿  #214 Parser drops trailing comments (open)
     #198 Crash on empty input (closed)
The latest is #214, "Parser drops trailing comments", which is still open.
```

## Adding servers

Servers are configured in JSON, in the same format as Claude Code, Claude Desktop and
most other MCP clients use:

```json
{
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"}
    },
    "docs": {
      "type": "http",
      "url": "https://example.com/mcp",
      "headers": {"Authorization": "Bearer ${DOCS_TOKEN}"}
    }
  }
}
```

Loom reads, in this order, later files overriding earlier ones for a server of the same
name:

1. `~/.loom/mcp.json`, your servers for every project;
2. `.mcp.json` in the project root, servers that come with the project;
3. files given with `--mcp-config FILE` (any number of times).

A server either runs as a command that loom starts (`command`, `args`, and optionally
`env` and `cwd`), talking over stdin and stdout, or is at a `url` (with optional
`headers`), using MCP's streamable HTTP transport. A server with both a `command` and a
`url` is an error. Server names use letters, digits, `_`, `.` and `-`, and may not contain
`__`, which separates the server from the tool in the name the model sees.
`"timeout": SECONDS` changes how long a tool call may take (5 minutes by default), and
`"disabled": true` skips a server.

`${VAR}` in any string is replaced by that environment variable, and `${VAR:-default}`
falls back to a default, so keep tokens in the environment (or in
`~/.loom/credentials.json`, see [models.md](models.md)) rather than in the file.

Loom connects to the servers when it starts in agent mode, or when you switch to it.
The announcements list them, like `MCP: github (26 tools), docs (failed)`.

## Google Stitch

[Google Stitch](https://stitch.withgoogle.com) designs UI screens from a description and
gives back each screen's HTML (with Tailwind CSS) and a screenshot. Loom has it built in:
set `STITCH_API_KEY` and the agent designs websites and apps with Stitch instead of
writing layouts from scratch.

1. Sign in at [stitch.withgoogle.com](https://stitch.withgoogle.com), open your profile's
   Settings and create an API key.
2. Put it in `~/.loom/credentials.json` (or the environment, or a `.env` file):

   ```json
   {"STITCH_API_KEY": "..."}
   ```

3. Start loom. The announcements show `MCP: stitch (15 tools)`.

```
> design a landing page for my bakery, warm and hand-made, with the menu and opening hours
● stitch - create_project (MCP)(title: "Crumb & Co.")
● stitch - generate_screen_from_text (MCP)(projectId: "4044680601076201931", prompt: "A warm, hand-made…", deviceType: "DESKTOP")
● Stitch(site/index.html)
  ⎿  Saved the Stitch screen to site/index.html
```

When Stitch is connected, the agent is told how to design with it: one Stitch project per
product, a design system so every page matches, one detailed prompt per page (the product,
its audience, the style, colors and fonts, and each section with its real content), one
call per generation, which takes a few minutes, then `edit_screens` for changes and
`generate_variants` for options. It shows you each screen's screenshot link.

To build a design, the agent uses loom's own `save_stitch_screen` tool, offered while
Stitch is connected: it gets the screen from Stitch and writes its HTML to a file in the
project, as an edit (so it asks like any other edit). The agent then fits it to the
project's stack, keeping the design: its Tailwind classes, colors, fonts and spacing. In a
[project](project.md), the Design agent designs the screens and lists them in the
architecture document, and the Building agent saves and builds them.

The built-in server is

```json
{
  "type": "http",
  "url": "https://stitch.googleapis.com/mcp",
  "headers": {"X-Goog-Api-Key": "${STITCH_API_KEY}"},
  "timeout": 600
}
```

A server named `stitch` in a config file replaces it, for example one that signs in with
OAuth (`"headers": {"Authorization": "Bearer ${STITCH_TOKEN}", "X-Goog-User-Project":
"my-gcp-project"}`), and `{"mcpServers": {"stitch": {"disabled": true}}}` turns it off.
Any server offering Stitch's tools gets the same help, whatever it's called.

Stitch's tools ask before they run, like other MCP tools. To let them run without asking:

```bash
loom --allow "mcp(stitch)"
```

`--yes-always` doesn't approve them, so give that rule to a `/project` run with
`--yes-always`, or the Design agent can't use Stitch.

## Using the tools

The model sees each tool as `mcp__<server>__<tool>`, with the description and parameters
the server gives, and any instructions the server has for using it are added to the
system prompt.

- `/mcp` shows each server, whether it's connected, and why not if it failed.
- `/mcp tools` lists the tools, or `/mcp tools SERVER` one server's.
- `/mcp connect SERVER` connects again, for example after fixing its config or restarting
  it.

A tool's result goes to the model as text. Images and binary resources are mentioned but
not shown to it.

## Permissions

MCP tools can do anything their server can, so like shell commands they ask before they
run, in the ask and accept-edits modes. Answering **always** allows that tool from then
on, saved in `.loom.permissions.json`. Allow rules work as for the built-in tools (see
[agent.md](agent.md#allow-rules)):

```bash
loom --allow "mcp(github)"                # every tool of the github server
loom --allow "mcp(github__get_*)"         # the github tools whose names start with get_
```

In plan mode every MCP tool asks before running: loom doesn't trust a server's own
`readOnlyHint`, since a mislabeled or hostile tool could claim to be safe when it isn't.
To let specific tools run without asking in plan mode, list them in
`~/.loom/mcp-readonly.json`:

```json
{"allow": ["mcp(github__get_*)", "mcp(docs__search)"]}
```

`--yes-always` doesn't approve MCP tools; they need a rule.

### Project servers

A project's `.mcp.json` comes with its code, and starting a server runs a command. So
loom asks before starting each server from it:

```
command: npx -y @modelcontextprotocol/server-github
env: GITHUB_PERSONAL_ACCESS_TOKEN=${GITHUB_TOKEN}
Start the MCP server 'github' from this project's .mcp.json? (Y)es/(N)o/(A)lways: trust it in this project [Yes]:
```

The question shows everything that decides what runs and what gets sent: the command or
url, and each `env` variable, header and `cwd`, as written in the file, so a token it
would pass on shows up as its `${VARIABLE}`.

**Always** remembers the answer in `~/.loom/mcp-approvals.json`, until the server's
config in `.mcp.json` changes. A server you decline can be started later with
`/mcp connect SERVER`.

The same goes for any config file inside the project, even when `.mcp.json` is a symlink
to it, and for `mcp-config` files named by a `.loom.conf.yml` or `.env` that came with
the repo. Servers in `~/.loom/mcp.json`, and in `--mcp-config` files you give outside the
project, are yours, so they start without asking.

## Limits

- Only tools are used. Resources and prompts that servers offer aren't, yet.
- HTTP servers that need OAuth sign-in aren't supported; use a server that takes a token
  in a header.
- The older HTTP+SSE transport isn't supported; most servers also offer streamable HTTP.
- `--no-mcp` turns MCP off.
