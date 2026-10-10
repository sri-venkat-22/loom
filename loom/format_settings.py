# Options whose whole value is a secret, and options whose values are NAME=secret pairs
SECRET_OPTIONS = ("openai_api_key", "anthropic_api_key", "analytics_posthog_project_api_key")
PAIR_OPTIONS = ("api_key", "set_env")


def mask(value):
    return f"...{value[-4:]}" if len(value) > 8 else "..."


def scrub_sensitive_info(args, text):
    """Mask the API keys and --set-env values in text, like a command line or a settings
    dump, keeping only the last 4 characters of long values."""
    if not text:
        return text
    for name in SECRET_OPTIONS:
        value = getattr(args, name, None)
        if value:
            text = text.replace(value, mask(value))
    for name in PAIR_OPTIONS:
        for setting in getattr(args, name, None) or []:
            key, sep, value = str(setting).partition("=")
            if sep and value.strip():
                text = text.replace(setting, f"{key}={mask(value.strip())}")
    return text


def format_settings(parser, args):
    show = scrub_sensitive_info(args, parser.format_values())
    # clean up the headings for consistency w/ new lines
    heading_env = "Environment Variables:"
    heading_defaults = "Defaults:"
    if heading_env in show:
        show = show.replace(heading_env, "\n" + heading_env)
        show = show.replace(heading_defaults, "\n" + heading_defaults)
    show += "\n"
    show += "Option settings:\n"
    for arg, val in sorted(vars(args).items()):
        if val:
            val = scrub_sensitive_info(args, str(val))
        show += f"  - {arg}: {val}\n"  # noqa: E221
    return show
