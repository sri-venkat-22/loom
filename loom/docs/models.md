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
| NVIDIA NIM | `NVIDIA_NIM_API_KEY` | `nvidia_nim/<model>`, `deepseek-flash` |
| xAI | `XAI_API_KEY` | `xai/grok-3-beta` |

For any other variable a provider needs, use `--set-env NAME=value`.

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
