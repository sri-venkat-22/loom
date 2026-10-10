from types import SimpleNamespace

from loom.format_settings import scrub_sensitive_info


def make_args(**kwargs):
    defaults = dict(
        openai_api_key=None,
        anthropic_api_key=None,
        analytics_posthog_project_api_key=None,
        api_key=None,
        set_env=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_masks_every_secret_on_the_command_line():
    args = make_args(
        openai_api_key="sk-openai-0123456789abcd",
        api_key=["deepseek=ds-secret-0123456789wxyz"],
        set_env=["MY_TOKEN=tok-0123456789qrst"],
        analytics_posthog_project_api_key="phc_0123456789mnop",
    )
    cmd_line = (
        "loom --openai-api-key sk-openai-0123456789abcd --api-key"
        " deepseek=ds-secret-0123456789wxyz --set-env MY_TOKEN=tok-0123456789qrst"
        " --analytics-posthog-project-api-key phc_0123456789mnop"
    )

    scrubbed = scrub_sensitive_info(args, cmd_line)

    for secret in ("sk-openai-0123", "ds-secret-0123", "tok-0123", "phc_0123"):
        assert secret not in scrubbed
    assert "deepseek=...wxyz" in scrubbed
    assert "MY_TOKEN=...qrst" in scrubbed


def test_masks_secrets_in_a_settings_dump():
    args = make_args(api_key=["anthropic=sk-ant-0123456789abcd"], set_env=["A=short"])
    dump = "  - api_key: ['anthropic=sk-ant-0123456789abcd']\n  - set_env: ['A=short']\n"

    scrubbed = scrub_sensitive_info(args, dump)

    assert "sk-ant-0123" not in scrubbed
    assert "short" not in scrubbed
    assert "A=..." in scrubbed


def test_leaves_text_without_secrets_alone():
    args = make_args(set_env=["MALFORMED"])
    assert scrub_sensitive_info(args, "loom --model gpt-4o") == "loom --model gpt-4o"
    assert scrub_sensitive_info(args, "") == ""
