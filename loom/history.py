import argparse

from loom import models, prompts
from loom.dump import dump  # noqa: F401


def looks_like_transcript(summary):
    """Some models carry on the conversation they were given instead of summarizing it."""
    if summary.lstrip().startswith("(Called "):
        return True
    return any(marker in summary for marker in ("# USER\n", "# ASSISTANT\n", "# TOOL RESULT\n"))


class ChatSummary:
    def __init__(self, models=None, max_tokens=1024):
        if not models:
            raise ValueError("At least one model must be provided")
        self.models = models if isinstance(models, list) else [models]
        self.max_tokens = max_tokens
        self.token_count = self.models[0].token_count

    def too_big(self, messages):
        sized = self.tokenize(messages)
        total = sum(tokens for tokens, _msg in sized)
        return total > self.max_tokens

    def tokenize(self, messages):
        sized = []
        for msg in messages:
            tokens = self.token_count(msg)
            sized.append((tokens, msg))
        return sized

    def summarize(self, messages, depth=0):
        messages = self.summarize_real(messages)
        if messages and messages[-1]["role"] != "assistant":
            messages.append(dict(role="assistant", content="Ok."))
        return messages

    def summarize_real(self, messages, depth=0):
        if not self.models:
            raise ValueError("No models available for summarization")

        sized = self.tokenize(messages)
        total = sum(tokens for tokens, _msg in sized)
        if total <= self.max_tokens and depth == 0:
            return messages

        min_split = 4
        if len(messages) <= min_split or depth > 3:
            return self.summarize_all(messages)

        tail_tokens = 0
        split_index = len(messages)
        half_max_tokens = self.max_tokens // 2

        # Iterate over the messages in reverse order
        for i in range(len(sized) - 1, -1, -1):
            tokens, _msg = sized[i]
            if tail_tokens + tokens < half_max_tokens:
                tail_tokens += tokens
                split_index = i
            else:
                break

        # Ensure the head ends with an assistant message. Not one that calls tools: the tail
        # would then start with tool results whose calls were summarized away.
        def ends_exchange(msg):
            return msg["role"] == "assistant" and not msg.get("tool_calls")

        while not ends_exchange(messages[split_index - 1]) and split_index > 1:
            split_index -= 1

        if split_index <= min_split:
            return self.summarize_all(messages)

        # Split head and tail
        tail = messages[split_index:]

        # Only size the head once
        sized_head = sized[:split_index]

        # Precompute token limit (fallback to 4096 if undefined)
        model_max_input_tokens = self.models[0].info.get("max_input_tokens") or 4096
        model_max_input_tokens -= 512  # reserve buffer for safety

        keep = []
        total = 0

        # Iterate in original order, summing tokens until limit
        for tokens, msg in sized_head:
            total += tokens
            if total > model_max_input_tokens:
                break
            keep.append(msg)
        # No need to reverse lists back and forth

        summary = self.summarize_all(keep)

        # If the combined summary and tail still fits, return directly
        summary_tokens = self.token_count(summary)
        tail_tokens = sum(tokens for tokens, _ in sized[split_index:])
        if summary_tokens + tail_tokens < self.max_tokens:
            return summary + tail

        # Otherwise recurse with increased depth
        return self.summarize_real(summary + tail, depth + 1)

    def summarize_all(self, messages, prompt=None, prefix=None):
        """Summarize messages as one user message. prompt and prefix replace the default
        instructions to the model and the text put before its summary."""
        content = ""
        for msg in messages:
            role = msg["role"].upper()
            if role not in ("USER", "ASSISTANT", "TOOL"):
                continue
            text = msg.get("content")
            text = text if isinstance(text, str) else ""
            if role == "TOOL":
                role = "TOOL RESULT"
                if len(text) > 1000:
                    # The end of a result often matters most, like a test run's summary
                    text = text[:400] + "\n...\n" + text[-600:]
            for call in msg.get("tool_calls") or []:
                function = call["function"]
                text += f"\n(Called {function['name']} with {function['arguments'][:300]})"
            content += f"# {role}\n"
            content += text
            if not content.endswith("\n"):
                content += "\n"

        # The instructions again after the transcript, so it isn't taken for a conversation
        # to carry on
        content = f"<transcript>\n{content}</transcript>\n\n{prompts.summarize_now}"
        summarize_messages = [
            dict(role="system", content=prompt or prompts.summarize),
            dict(role="user", content=content),
        ]

        for model in self.models:
            try:
                summary = model.simple_send_with_retries(summarize_messages)
                if summary and not looks_like_transcript(summary):
                    summary = (prompts.summary_prefix if prefix is None else prefix) + summary
                    return [dict(role="user", content=summary)]
            except Exception as e:
                print(f"Summarization failed for model {model.name}: {str(e)}")

        raise ValueError("summarizer unexpectedly failed for all models")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("filename", help="Markdown file to parse")
    args = parser.parse_args()

    model_names = ["gpt-3.5-turbo", "gpt-4"]  # Add more model names as needed
    model_list = [models.Model(name) for name in model_names]
    summarizer = ChatSummary(model_list)

    with open(args.filename, "r") as f:
        text = f.read()

    summary = summarizer.summarize_chat_history_markdown(text)
    dump(summary)


if __name__ == "__main__":
    main()
