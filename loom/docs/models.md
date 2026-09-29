# Models and API keys

Loom talks to models through [LiteLLM](https://docs.litellm.ai/docs/providers), so it
can use almost any hosted or local LLM. Pick one with `--model` at startup or `/model`
in the chat.

## Setting API keys

Use whichever is convenient; they all end up as environment variables:

```bash
# on the command line (sets ANTHROPIC_API_KEY)
loom --model sonnet --api-key anthropic=<key>

# in the shell
export ANTHROPIC_API_KEY=<key>

# in a .env file in your repo root or home directory
ANTHROPIC_API_KEY=<key>

# in ~/.loom/credentials.json, a JSON object of variables
{"NVIDIA_NIM_API_KEY": "nvapi-..."}

# in .loom.conf.yml
api-key:
  - anthropic=<key>
  - gemini=<key>
```

`--api-key provider=<key>` sets `PROVIDER_API_KEY`. Common ones:

| Provider | Environment variable | Example model |
|---|---|---|
| Anthropic | `ANTHROPIC_API_KEY` | `sonnet`, `opus`, `haiku` |
| OpenAI | `OPENAI_API_KEY` | `gpt-4o`, `o3-mini` |
| Google Gemini | `GEMINI_API_KEY` | `gemini-3.1`, `flash` |
| OpenRouter | `OPENROUTER_API_KEY` | `openrouter/<vendor>/<model>`, `nemotron` |
| DeepSeek | `DEEPSEEK_API_KEY` | `deepseek`, `r1` |
| NVIDIA build (NIM) | `NVIDIA_NIM_API_KEY` | `nemotron`, `nemotron-super`, `nvidia_nim/<vendor>/<model>` |
| xAI | `XAI_API_KEY` | `xai/grok-3-beta` |

For any other variable a provider needs, use `--set-env NAME=value`.

Keys in `~/.loom/credentials.json` are loaded first, so a `.env` file can override them.

## Choosing a model

Short names such as `sonnet`, `gemini-3.1` or `deepseek` are listed in
[model-aliases.md](model-aliases.md). Otherwise use the LiteLLM model name, usually
`provider/model`. Search the models loom knows about with `/models <text>` in the chat
or `loom --list-models <text>`.

Loom uses up to three models:

- **main model** (`--model`, `/model`): writes the code.
- **weak model** (`--weak-model`, `/weak-model`): writes commit messages and summarizes
  chat history. Defaults to a cheap model from the same provider.
- **editor model** (`--editor-model`, `/editor-model`): turns the architect's plan into
  edits in architect mode.

Reasoning models accept `--reasoning-effort low|medium|high` or `--thinking-tokens 8k`
(also `/reasoning-effort` and `/think-tokens`), depending on what the model supports.
Thinking works with the agent's tools: loom sends the model's signed thinking back with
its tool calls, as Anthropic requires.

## Prompt caching

Some providers cache the start of a prompt, so resending it costs a fraction of the
price. OpenAI, DeepSeek and Gemini do this automatically. Anthropic models (directly,
on Bedrock or Vertex, or through OpenRouter) cache only what loom marks, which
`--cache-prompts` turns on. It's on by default for the agent, which resends the whole
conversation at every step: loom marks the newest message each time, so each step
reads everything before it from the cache. `--no-cache-prompts` turns it off.

The cost report after each request counts the cached tokens, like
`Tokens: 12k sent, 9.1k cache hit, 300 received.` Anthropic only caches prompts above
a minimum size (1,024 to 4,096 tokens, depending on the model), so short requests
don't benefit.

## NVIDIA build

[build.nvidia.com](https://build.nvidia.com) serves many open models free for
development (rate limited), with an `nvapi-...` key from your NVIDIA account. `nemotron`
(Nemotron 3 Ultra) is set up for the agent: tool calling, a 256K context, thinking on,
and Nemotron 3 Super (`nemotron-super`) writing commit messages. It's picked by default
when `NVIDIA_NIM_API_KEY` is the only key set; to use it everywhere, put
`model: nemotron` in `~/.loom.conf.yml`.

Other models from `https://integrate.api.nvidia.com/v1/models` work as
`nvidia_nim/<vendor>/<model>`, but loom doesn't know their context size or whether they
can call tools, so it warns at startup and uses the classic edit format. Pass
`--edit-format agent` to use the agent with one that supports tool calling.

## Models in agent mode

The agent needs a model that makes tool calls through the API, and makes them well
enough to finish a task. `scripts/check_agent_models.py` checks this for any model you
have a key for:

```bash
python scripts/check_agent_models.py nemotron deepseek-flash gemini-3.1 --log-dir /tmp/agent-check
```

For each model it runs a **probe**, one request asking for two file reads, which passes
if the model calls `read_file` through the API with valid arguments; then a **task**, a
real agent run in a scratch git repo: find the bug that makes `check_calc.py` fail, fix
it, and run the check with bash. It prints a table like the one below. `--log-dir`
keeps each task's transcript.

Checked on 2026-09-27:

| Alias | Model | Agent mode |
|---|---|---|
| `nemotron` | `nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b` | **Works.** Three runs, streamed and not: two parallel calls in the probe every time, and the task done in 3 steps, in 13 to 34 seconds. NVIDIA sometimes cuts a reply off with "Service temporarily overloaded", which loom retries. |
| `deepseek-flash` | `nvidia_nim/deepseek-ai/deepseek-v4.1-flash` | **Not usable on NVIDIA right now.** Every request, with or without tools, ended in a 504 gateway timeout after 15 minutes, so its tool calling couldn't be judged. Loom doesn't know whether it supports tools, so it uses the `whole` edit format, not the agent. The alias used to point at `deepseek-v4-flash-0731`, which NVIDIA retired (HTTP 410). |
| `gemini-3.1` | `gemini/gemini-3.1-pro-preview` | **Expected to work; not yet run against the live API.** Loom uses the agent for it. Offline tests (`tests/basic/test_gemini_agent.py`) run the agent through litellm's real Gemini translation, streamed and not, and check that each function call goes back with the thought signature Gemini 3 requires. Run the script with `GEMINI_API_KEY` set to confirm. |

### Falling back to edit formats

A model that can't make tool calls still works with loom's classic edit formats, where
it edits files by replying with edit blocks ([Edit formats](#edit-formats)):

- Loom only starts in agent mode when the model is known to support tool calling
  (`supports_function_calling` in its metadata). Otherwise it uses the model's edit
  format. `--no-agent` does the same for any model, and `/chat-mode diff` or `/code`
  switch mid-session.
- If the provider rejects the tools anyway (errors like "does not support tools"), loom
  switches to the model's edit format and sends the request again.
- If the model writes a tool call as text instead of making it, loom asks it once to use
  tool calling, then suggests `/chat-mode diff`.

To use the agent with a model loom doesn't know supports tools, pass
`--edit-format agent`, or set `"supports_function_calling": true` in a
`.loom.model.metadata.json` file (see below).

## Local and self-hosted models

```bash
# Ollama
export OLLAMA_API_BASE=http://127.0.0.1:11434
loom --model ollama_chat/qwen2.5-coder:32b

# any OpenAI-compatible server (vLLM, LM Studio, llama.cpp server, ...)
loom --model openai/<model-name> --openai-api-base http://localhost:8000/v1 --api-key openai=<key-or-dummy>
```

Local models often default to a small context window. If replies get cut off or loom
warns about unknown context size, set the window size in a metadata file (below).

## Settings for unknown models

When loom does not know a model it warns and falls back to generic settings. You can
describe the model yourself:

- **`.loom.model.metadata.json`**: context window and pricing, in LiteLLM's format:

  ```json
  {
    "openai/my-local-model": {
      "max_input_tokens": 32768,
      "max_output_tokens": 8192,
      "input_cost_per_token": 0,
      "output_cost_per_token": 0,
      "litellm_provider": "openai",
      "mode": "chat"
    }
  }
  ```

- **`.loom.model.settings.yml`**: how loom drives the model (edit format, weak model,
  extra request parameters):

  ```yaml
  - name: openai/my-local-model
    edit_format: diff
    use_repo_map: true
    weak_model_name: openai/my-local-model
    extra_params:
      max_tokens: 8192
  ```

Loom looks for both files in your home directory, the git root and the current
directory, or at paths given with `--model-metadata-file` and `--model-settings-file`.

## Edit formats

The edit format is how the model writes changes: `whole` (rewrite whole files), `diff`
(search/replace blocks), `diff-fenced`, `udiff`, and `patch`. Each known model has a
default that works well for it. Override it with `--edit-format` if a model keeps
producing edits loom cannot apply.
