#!/usr/bin/env python

import json
import os
import shutil
import warnings
from pathlib import Path

import importlib_resources

from loom import __version__, urls, utils
from loom.dump import dump  # noqa: F401

warnings.simplefilter("ignore", category=FutureWarning)


def install_help_extra(io):
    pip_install_cmd = [
        *utils.loom_extra("help"),
        "--extra-index-url",
        "https://download.pytorch.org/whl/cpu",
    ]
    res = utils.check_pip_install_extra(
        io,
        "llama_index.embeddings.huggingface",
        "To use interactive /help you need to install the help extras",
        pip_install_cmd,
    )
    return res


def get_package_files():
    for path in importlib_resources.files("loom.docs").iterdir():
        if path.is_file() and path.name.endswith(".md"):
            yield path


def fname_to_url(filepath):
    """Map a file under loom/docs/ to its page on GitHub, or "" if it isn't a doc."""
    parts = [p for p in filepath.replace("\\", "/").split("/") if p]

    # Use the last loom/docs pair, in case "loom" or "docs" also appear higher up the path
    for i in range(len(parts) - 2, -1, -1):
        if parts[i].lower() == "loom" and parts[i + 1].lower() == "docs":
            rel = parts[i + 2 :]
            if rel and rel[-1].lower().endswith(".md"):
                return f"{urls.docs}/" + "/".join(rel)
            return ""

    return ""


def get_index():
    from llama_index.core import (
        Document,
        StorageContext,
        VectorStoreIndex,
        load_index_from_storage,
    )
    from llama_index.core.node_parser import MarkdownNodeParser

    dname = Path.home() / ".loom" / "caches" / ("help." + __version__)

    index = None
    try:
        if dname.exists():
            storage_context = StorageContext.from_defaults(
                persist_dir=dname,
            )
            index = load_index_from_storage(storage_context)
    except (OSError, json.JSONDecodeError):
        shutil.rmtree(dname)

    if index is None:
        parser = MarkdownNodeParser()

        nodes = []
        for fname in get_package_files():
            doc = Document(
                text=fname.read_text(encoding="utf-8"),
                metadata=dict(
                    filename=fname.name,
                    extension=fname.suffix,
                    url=fname_to_url(str(fname)),
                ),
            )
            nodes += parser.get_nodes_from_documents([doc])

        index = VectorStoreIndex(nodes, show_progress=True)
        dname.parent.mkdir(parents=True, exist_ok=True)
        index.storage_context.persist(dname)

    return index


class Help:
    def __init__(self):
        from llama_index.core import Settings
        from llama_index.embeddings.huggingface import HuggingFaceEmbedding

        os.environ["TOKENIZERS_PARALLELISM"] = "true"
        Settings.embed_model = HuggingFaceEmbedding(model_name="BAAI/bge-small-en-v1.5")

        index = get_index()

        self.retriever = index.as_retriever(similarity_top_k=20)

    def ask(self, question):
        nodes = self.retriever.retrieve(question)

        context = f"""# Question: {question}

# Relevant docs:

"""  # noqa: E231

        for node in nodes:
            url = node.metadata.get("url", "")
            if url:
                url = f' from_url="{url}"'

            context += f"<doc{url}>\n"
            context += node.text
            context += "\n</doc>\n\n"

        return context
