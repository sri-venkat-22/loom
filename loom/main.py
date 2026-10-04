import atexit
import hashlib
import json
import os
import re
import sys
import threading
import traceback
import webbrowser
from dataclasses import fields
from pathlib import Path

try:
    import git
except ImportError:
    git = None

import importlib_resources
import shtab
from dotenv import dotenv_values, load_dotenv
from prompt_toolkit.enums import EditingMode

from loom import __version__, models, urls, utils
from loom.analytics import Analytics
from loom.args import get_parser
from loom.coders import Coder
from loom.coders.base_coder import UnknownEditFormat
from loom.commands import Commands, SwitchCoder
from loom.copypaste import ClipboardWatcher
from loom.deprecated import handle_deprecated_model_args
from loom.format_settings import format_settings, scrub_sensitive_info
from loom.history import ChatSummary
from loom.hooks import Hooks
from loom.io import InputOutput
from loom.llm import litellm  # noqa: F401; properly init litellm on launch
from loom.mcp import McpError, McpManager
from loom.models import ModelSettings
from loom.onboarding import offer_openrouter_oauth, select_default_model
from loom.permissions import SETTINGS_FILE, Permissions
from loom.repo import ANY_GIT_ERROR, GitRepo
from loom.report import report_uncaught_exceptions
from loom.sessions import SESSIONS_DIR, Session, SessionError
from loom.versioncheck import check_version, install_from_main_branch, install_upgrade
from loom.watch import FileWatcher

from .dump import dump  # noqa: F401


def check_config_files_for_yes(config_files):
    found = False
    for config_file in config_files:
        if Path(config_file).exists():
            try:
                with open(config_file, "r") as f:
                    for line in f:
                        if line.strip().startswith("yes:"):
                            print("Configuration error detected.")
                            print(f"The file {config_file} contains a line starting with 'yes:'")
                            print("Please replace 'yes:' with 'yes-always:' in this file.")
                            found = True
            except Exception:
                pass
    return found


# Options that make loom run a command: lint and test commands run after the agent's edits,
# --load runs slash commands (like /run) at startup
PROJECT_COMMAND_OPTIONS = ("lint_cmd", "test_cmd", "load", "notifications_command", "editor")

# Options a repo mustn't set for itself, since they let loom act without asking: allow
# rules, MCP config files whose servers loom starts, and the command options
PROJECT_GATED_OPTIONS = ("allow", "mcp_config") + PROJECT_COMMAND_OPTIONS


def config_approvals_file():
    return Path.home() / ".loom" / "config-approvals.json"


def safe_resolve(fname):
    try:
        return Path(fname).expanduser().resolve()
    except (OSError, RuntimeError):
        return Path(fname)


def project_sourced_options(parser, args, loaded_dotenvs):
    """{option: file} for the PROJECT_GATED_OPTIONS whose value came with the repo: from a
    .loom.conf.yml or .env found in the project, rather than the command line, the
    environment, --config or the user's own files in their home directory."""
    home = Path.home()
    trusted = {
        safe_resolve(home / ".loom.conf.yml"),
        safe_resolve(home / ".env"),
        safe_resolve(home / ".loom" / "oauth-keys.env"),
        safe_resolve(home / ".loom" / "credentials.json"),
    }
    if args.config:
        trusted.add(safe_resolve(args.config))

    res = {}
    for source, settings in parser.get_source_to_settings_dict().items():
        kind, _, fname = source.partition("|")
        for key, (action, _value) in settings.items():
            dest = action.dest if action else None
            if dest not in PROJECT_GATED_OPTIONS:
                continue
            if kind == "config_file" and safe_resolve(fname) not in trusted:
                res[dest] = fname
            elif kind == "environment_variables":
                # Set by a project .env file, which load_dotenv_files put in the environment
                for env_file in loaded_dotenvs:
                    if safe_resolve(env_file) in trusted:
                        continue
                    try:
                        if key in dotenv_values(env_file):
                            res[dest] = env_file
                    except (OSError, ValueError):
                        res[dest] = env_file
    return res


def approve_project_commands(io, parser, args, project_options, root):
    """Ask before using the PROJECT_COMMAND_OPTIONS that came with the repo, and remember
    "always" in ~/.loom/config-approvals.json until they change. Declined options go back
    to their defaults. Returns the declined options."""
    gated = [dest for dest in PROJECT_COMMAND_OPTIONS if dest in project_options]
    if not gated:
        return []
    settings = {dest: getattr(args, dest) for dest in gated}
    digest = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()
    key = str(safe_resolve(root))
    try:
        approvals = json.loads(config_approvals_file().read_text(encoding="utf-8"))
        approvals = approvals if isinstance(approvals, dict) else {}
    except (OSError, ValueError):
        approvals = {}
    if approvals.get(key) == digest:
        return []

    lines = []
    for dest, value in settings.items():
        for item in value if isinstance(value, list) else [value]:
            lines.append(f"{dest.replace('_', '-')}: {item}")
    sources = sorted({Path(project_options[dest]).name for dest in gated})
    answer = io.permission_ask(
        f"Run the commands set in this project's {' and '.join(sources)}?",
        subject="\n".join(lines),
        always="trust them in this project",
        explicit_yes_required=True,
    )
    if answer == "always":
        approvals[key] = digest
        try:
            config_approvals_file().parent.mkdir(parents=True, exist_ok=True)
            config_approvals_file().write_text(
                json.dumps(approvals, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as err:
            io.tool_warning(f"Unable to save the approval to {config_approvals_file()}: {err}")
    if answer in ("yes", "always"):
        return []

    for dest in gated:
        setattr(args, dest, parser.get_default(dest))
    if "test_cmd" in gated:
        args.auto_test = False
    if "notifications_command" in gated:
        io.notifications_command = (
            io.get_default_notification_command() if io.notifications else None
        )
    io.tool_output(
        f"loom won't use {', '.join(dest.replace('_', '-') for dest in gated)} from the project"
        " this session; it asks again next time."
    )
    return gated


def get_git_root():
    """Try and guess the git repo, since the conf.yml can be at the repo root"""
    try:
        repo = git.Repo(search_parent_directories=True)
        return repo.working_tree_dir
    except (git.InvalidGitRepositoryError, FileNotFoundError):
        return None


def guessed_wrong_repo(io, git_root, fnames, git_dname):
    """After we parse the args, we can determine the real repo. Did we guess wrong?"""

    try:
        check_repo = Path(GitRepo(io, fnames, git_dname).root).resolve()
    except (OSError,) + ANY_GIT_ERROR:
        return

    # we had no guess, rely on the "true" repo result
    if not git_root:
        return str(check_repo)

    git_root = Path(git_root).resolve()
    if check_repo == git_root:
        return

    return str(check_repo)


def make_new_repo(git_root, io):
    try:
        repo = git.Repo.init(git_root)
        check_gitignore(git_root, io, False)
    except ANY_GIT_ERROR as err:
        io.tool_error(f"Unable to create git repo in {git_root}")
        io.tool_output(str(err))
        return

    io.tool_output(f"Git repository created in {git_root}")
    return repo


def setup_git(git_root, io):
    if git is None:
        return

    try:
        cwd = Path.cwd()
    except OSError:
        cwd = None

    repo = None

    if git_root:
        try:
            repo = git.Repo(git_root)
        except ANY_GIT_ERROR:
            pass
    elif cwd == Path.home():
        io.tool_warning(
            "You should probably run loom in your project's directory, not your home dir."
        )
        return
    elif cwd and io.confirm_ask(
        "No git repo found, create one to track loom's changes (recommended)?"
    ):
        git_root = str(cwd.resolve())
        repo = make_new_repo(git_root, io)

    if not repo:
        return

    try:
        user_name = repo.git.config("--get", "user.name") or None
    except git.exc.GitCommandError:
        user_name = None

    try:
        user_email = repo.git.config("--get", "user.email") or None
    except git.exc.GitCommandError:
        user_email = None

    if user_name and user_email:
        return repo.working_tree_dir

    with repo.config_writer() as git_config:
        if not user_name:
            git_config.set_value("user", "name", "Your Name")
            io.tool_warning('Update git name with: git config user.name "Your Name"')
        if not user_email:
            git_config.set_value("user", "email", "you@example.com")
            io.tool_warning('Update git email with: git config user.email "you@example.com"')

    return repo.working_tree_dir


def check_gitignore(git_root, io, ask=True):
    if not git_root:
        return

    try:
        repo = git.Repo(git_root)
        patterns_to_add = []

        if not repo.ignored(".loom.chat.history.md"):
            # loom's history, caches and sessions, but not .loom/ with the custom commands
            # and hooks to share with the repo
            patterns_to_add += [".loom*", "!.loom/"]
        if not repo.ignored(".loom/worktrees/build"):
            # The parallel builders' checkouts (loom/worktrees.py)
            patterns_to_add.append(".loom/worktrees/")

        env_path = Path(git_root) / ".env"
        if env_path.exists() and not repo.ignored(".env"):
            patterns_to_add.append(".env")

        if not patterns_to_add:
            return

        gitignore_file = Path(git_root) / ".gitignore"
        if gitignore_file.exists():
            try:
                content = io.read_text(gitignore_file)
                if content is None:
                    return
                if not content.endswith("\n"):
                    content += "\n"
            except OSError as e:
                io.tool_error(f"Error when trying to read {gitignore_file}: {e}")
                return
        else:
            content = ""
    except ANY_GIT_ERROR:
        return

    if ask:
        io.tool_output("You can skip this check with --no-gitignore")
        if not io.confirm_ask(f"Add {', '.join(patterns_to_add)} to .gitignore (recommended)?"):
            return

    content += "\n".join(patterns_to_add) + "\n"

    try:
        io.write_text(gitignore_file, content)
        io.tool_output(f"Added {', '.join(patterns_to_add)} to .gitignore")
    except OSError as e:
        io.tool_error(f"Error when trying to write to {gitignore_file}: {e}")
        io.tool_output(
            "Try running with appropriate permissions or manually add these patterns to .gitignore:"
        )
        for pattern in patterns_to_add:
            io.tool_output(f"  {pattern}")


def check_streamlit_install(io):
    return utils.check_pip_install_extra(
        io,
        "streamlit",
        "You need to install the loom browser feature",
        utils.loom_extra("browser"),
    )


def check_web_install(io):
    return utils.check_pip_install_extra(
        io,
        "uvicorn",
        "loom --web needs a web server, from loom's web extra.",
        utils.loom_extra("web"),
    )


def start_web(io, args):
    """Serve the web UI and hand the chat to the browser. Returns False if it can't."""
    from loom.web.backend.server import ServerError, start_server

    try:
        url = start_server(io.web, port=args.port, open_browser=args.browser, io=io)
    except ServerError as err:
        io.tool_error(str(err))
        return False
    io.tool_output(f"loom is running at {url}")
    io.tool_output("Chat in your browser. Press ^C twice here to stop loom.")
    return True


def write_streamlit_credentials():
    from streamlit.file_util import get_streamlit_file_path

    # Streamlit asks for an email address on its first run; an empty one in its
    # credentials file skips that prompt.

    credential_path = Path(get_streamlit_file_path()) / "credentials.toml"
    if not os.path.exists(credential_path):
        empty_creds = '[general]\nemail = ""\n'

        os.makedirs(os.path.dirname(credential_path), exist_ok=True)
        with open(credential_path, "w") as f:
            f.write(empty_creds)
    else:
        print("Streamlit credentials already exist.")


def launch_gui(args):
    from streamlit.web import cli

    from loom import gui

    print()
    print("CONTROL-C to exit...")

    # Necessary so streamlit does not prompt the user for an email address.
    write_streamlit_credentials()

    target = gui.__file__

    st_args = ["run", target]

    st_args += [
        "--browser.gatherUsageStats=false",
        "--runner.magicEnabled=false",
        "--server.runOnSave=false",
    ]

    # Release builds run Streamlit without development mode or a file watcher
    is_dev = "-dev" in str(__version__)

    if is_dev:
        print("Watching for file changes.")
    else:
        st_args += [
            "--global.developmentMode=false",
            "--server.fileWatcherType=none",
            "--client.toolbarMode=viewer",  # minimal?
        ]

    st_args += ["--"] + args

    cli.main(st_args)

    # from click.testing import CliRunner
    # runner = CliRunner()
    # from streamlit.web import bootstrap
    # bootstrap.load_config_options(flag_options={})
    # cli.main_run(target, args)
    # sys.argv = ['streamlit', 'run', '--'] + args


def parse_lint_cmds(lint_cmds, io):
    err = False
    res = dict()
    for lint_cmd in lint_cmds:
        if re.match(r"^[a-z]+:.*", lint_cmd):
            pieces = lint_cmd.split(":")
            lang = pieces[0]
            cmd = lint_cmd[len(lang) + 1 :]
            lang = lang.strip()
        else:
            lang = None
            cmd = lint_cmd

        cmd = cmd.strip()

        if cmd:
            res[lang] = cmd
        else:
            io.tool_error(f'Unable to parse --lint-cmd "{lint_cmd}"')
            io.tool_output('The arg should be "language: cmd --args ..."')
            io.tool_output('For example: --lint-cmd "python: flake8 --select=E9"')
            err = True
    if err:
        return
    return res


def generate_search_path_list(default_file, git_root, command_line_file):
    files = []
    files.append(Path.home() / default_file)  # homedir
    if git_root:
        files.append(Path(git_root) / default_file)  # git root
    files.append(default_file)
    if command_line_file:
        files.append(command_line_file)

    resolved_files = []
    for fn in files:
        try:
            resolved_files.append(Path(fn).resolve())
        except OSError:
            pass

    files = resolved_files
    files.reverse()
    uniq = []
    for fn in files:
        if fn not in uniq:
            uniq.append(fn)
    uniq.reverse()
    files = uniq
    files = list(map(str, files))
    files = list(dict.fromkeys(files))

    return files


def register_models(git_root, model_settings_fname, io, verbose=False):
    model_settings_files = generate_search_path_list(
        ".loom.model.settings.yml", git_root, model_settings_fname
    )

    try:
        files_loaded = models.register_models(model_settings_files)
        if len(files_loaded) > 0:
            if verbose:
                io.tool_output("Loaded model settings from:")
                for file_loaded in files_loaded:
                    io.tool_output(f"  - {file_loaded}")  # noqa: E221
        elif verbose:
            io.tool_output("No model settings files loaded")
    except Exception as e:
        io.tool_error(f"Error loading loom model settings: {e}")
        return 1

    if verbose:
        io.tool_output("Searched for model settings files:")
        for file in model_settings_files:
            io.tool_output(f"  - {file}")

    return None


def load_credentials_file(fname):
    """Set environment variables from a JSON object of NAME: value pairs, like
    {"NVIDIA_NIM_API_KEY": "nvapi-..."}. Returns whether the file was loaded."""
    try:
        data = json.loads(Path(fname).read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        print(f"Error loading {fname}: {err}")
        return False
    if not isinstance(data, dict):
        print(f"Error loading {fname}: expected a JSON object of NAME: value pairs")
        return False

    for name, value in data.items():
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(name)) and isinstance(value, str):
            os.environ[name] = value
    return True


def load_dotenv_files(git_root, dotenv_fname, encoding="utf-8"):
    # Standard .env file search path
    dotenv_files = generate_search_path_list(
        ".env",
        git_root,
        dotenv_fname,
    )

    # Explicitly add the OAuth keys file to the beginning of the list
    oauth_keys_file = Path.home() / ".loom" / "oauth-keys.env"
    if oauth_keys_file.exists():
        # Insert at the beginning so it's loaded first (and potentially overridden)
        dotenv_files.insert(0, str(oauth_keys_file.resolve()))
        # Remove duplicates if it somehow got included by generate_search_path_list
        dotenv_files = list(dict.fromkeys(dotenv_files))

    loaded = []

    # API keys kept as JSON, loaded first so any .env file can override them
    credentials_file = Path.home() / ".loom" / "credentials.json"
    if credentials_file.exists() and load_credentials_file(credentials_file):
        loaded.append(str(credentials_file.resolve()))

    for fname in dotenv_files:
        try:
            if Path(fname).exists():
                load_dotenv(fname, override=True, encoding=encoding)
                loaded.append(fname)
        except OSError as e:
            print(f"OSError loading {fname}: {e}")
        except Exception as e:
            print(f"Error loading {fname}: {e}")
    return loaded


def register_litellm_models(git_root, model_metadata_fname, io, verbose=False):
    model_metadata_files = []

    # Add the resource file path
    resource_metadata = importlib_resources.files("loom.resources").joinpath("model-metadata.json")
    model_metadata_files.append(str(resource_metadata))

    model_metadata_files += generate_search_path_list(
        ".loom.model.metadata.json", git_root, model_metadata_fname
    )

    try:
        model_metadata_files_loaded = models.register_litellm_models(model_metadata_files)
        if len(model_metadata_files_loaded) > 0 and verbose:
            io.tool_output("Loaded model metadata from:")
            for model_metadata_file in model_metadata_files_loaded:
                io.tool_output(f"  - {model_metadata_file}")  # noqa: E221
    except Exception as e:
        io.tool_error(f"Error loading model metadata models: {e}")
        return 1


def sanity_check_repo(repo, io):
    if not repo:
        return True

    if not repo.repo.working_tree_dir:
        io.tool_error("The git repo does not seem to have a working tree?")
        return False

    bad_ver = False
    try:
        repo.get_tracked_files()
        if not repo.git_repo_error:
            return True
        error_msg = str(repo.git_repo_error)
    except UnicodeDecodeError as exc:
        error_msg = (
            "Failed to read the Git repository. This issue is likely caused by a path encoded "
            f'in a format different from the expected encoding "{sys.getfilesystemencoding()}".\n'
            f"Internal error: {str(exc)}"
        )
    except ANY_GIT_ERROR as exc:
        error_msg = str(exc)
        bad_ver = "version in (1, 2)" in error_msg
    except AssertionError as exc:
        error_msg = str(exc)
        bad_ver = True

    if bad_ver:
        io.tool_error("Loom only works with git repos with version number 1 or 2.")
        io.tool_output("You may be able to convert your repo: git update-index --index-version=2")
        io.tool_output("Or run loom --no-git to proceed without using git.")
        io.offer_url(urls.git_index_version, "Open documentation url for more info?")
        return False

    io.tool_error("Unable to read git repository, it may be corrupt?")
    io.tool_output(error_msg)
    return False


def main(argv=None, input=None, output=None, force_git_root=None, return_coder=False):
    report_uncaught_exceptions()

    if argv is None:
        argv = sys.argv[1:]

    if git is None:
        git_root = None
    elif force_git_root:
        git_root = force_git_root
    else:
        git_root = get_git_root()

    conf_fname = Path(".loom.conf.yml")

    default_config_files = []
    try:
        default_config_files += [conf_fname.resolve()]  # CWD
    except OSError:
        pass

    if git_root:
        git_conf = Path(git_root) / conf_fname  # git root
        if git_conf not in default_config_files:
            default_config_files.append(git_conf)
    default_config_files.append(Path.home() / conf_fname)  # homedir
    default_config_files = list(map(str, default_config_files))

    parser = get_parser(default_config_files, git_root)
    try:
        args, unknown = parser.parse_known_args(argv)
    except AttributeError as e:
        if all(word in str(e) for word in ["bool", "object", "has", "no", "attribute", "strip"]):
            if check_config_files_for_yes(default_config_files):
                return 1
        raise e

    if args.verbose:
        print("Config files search order, if no --config:")
        for file in default_config_files:
            exists = "(exists)" if Path(file).exists() else ""
            print(f"  - {file} {exists}")

    default_config_files.reverse()

    parser = get_parser(default_config_files, git_root)

    args, unknown = parser.parse_known_args(argv)

    # Load the .env file specified in the arguments
    loaded_dotenvs = load_dotenv_files(git_root, args.env_file, args.encoding)

    # Parse again to include any arguments that might have been defined in .env
    args = parser.parse_args(argv)
    project_options = project_sourced_options(parser, args, loaded_dotenvs)

    if args.shell_completions:
        # Ensure parser.prog is set for shtab, though it should be by default
        parser.prog = "loom"
        print(shtab.complete(parser, shell=args.shell_completions))
        sys.exit(0)

    if git is None:
        args.git = False

    if args.analytics_disable:
        analytics = Analytics(permanently_disable=True)
        print("Analytics have been permanently disabled.")

    if not args.verify_ssl:
        import httpx

        os.environ["SSL_VERIFY"] = ""
        litellm._load_litellm()
        litellm._lazy_module.client_session = httpx.Client(verify=False)
        litellm._lazy_module.aclient_session = httpx.AsyncClient(verify=False)
        # Set verify_ssl on the model_info_manager
        models.model_info_manager.set_verify_ssl(False)

    if args.timeout:
        models.request_timeout = args.timeout

    if args.dark_mode:
        args.user_input_color = "#32FF32"
        args.tool_error_color = "#FF3333"
        args.tool_warning_color = "#FFFF00"
        args.assistant_output_color = "#00FFFF"
        args.code_theme = "monokai"

    if args.light_mode:
        args.user_input_color = "green"
        args.tool_error_color = "red"
        args.tool_warning_color = "#FFA500"
        args.assistant_output_color = "blue"
        args.code_theme = "default"

    if return_coder and args.yes_always is None:
        args.yes_always = True

    editing_mode = EditingMode.VI if args.vim else EditingMode.EMACS

    if args.web:
        from loom.web.backend.webio import WebIO

        io_class = WebIO
    else:
        io_class = InputOutput

    def get_io(pretty):
        return io_class(
            pretty,
            args.yes_always,
            args.input_history_file,
            args.chat_history_file,
            input=input,
            output=output,
            user_input_color=args.user_input_color,
            tool_output_color=args.tool_output_color,
            tool_warning_color=args.tool_warning_color,
            tool_error_color=args.tool_error_color,
            completion_menu_color=args.completion_menu_color,
            completion_menu_bg_color=args.completion_menu_bg_color,
            completion_menu_current_color=args.completion_menu_current_color,
            completion_menu_current_bg_color=args.completion_menu_current_bg_color,
            assistant_output_color=args.assistant_output_color,
            code_theme=args.code_theme,
            dry_run=args.dry_run,
            encoding=args.encoding,
            line_endings=args.line_endings,
            llm_history_file=args.llm_history_file,
            editingmode=editing_mode,
            fancy_input=args.fancy_input,
            multiline_mode=args.multiline,
            notifications=args.notifications,
            notifications_command=args.notifications_command,
        )

    io = get_io(args.pretty)
    try:
        io.rule()
    except UnicodeEncodeError as err:
        if not io.pretty:
            raise err
        io = get_io(False)
        io.tool_warning("Terminal does not support pretty output (UnicodeDecodeError)")

    # Process any environment variables set via --set-env
    if args.set_env:
        for env_setting in args.set_env:
            try:
                name, value = env_setting.split("=", 1)
                os.environ[name.strip()] = value.strip()
            except ValueError:
                io.tool_error(f"Invalid --set-env format: {env_setting}")
                io.tool_output("Format should be: ENV_VAR_NAME=value")
                return 1

    # Process any API keys set via --api-key
    if args.api_key:
        for api_setting in args.api_key:
            try:
                provider, key = api_setting.split("=", 1)
                env_var = f"{provider.strip().upper()}_API_KEY"
                os.environ[env_var] = key.strip()
            except ValueError:
                io.tool_error(f"Invalid --api-key format: {api_setting}")
                io.tool_output("Format should be: provider=key")
                return 1

    if args.anthropic_api_key:
        os.environ["ANTHROPIC_API_KEY"] = args.anthropic_api_key

    if args.openai_api_key:
        os.environ["OPENAI_API_KEY"] = args.openai_api_key

    # Handle deprecated model shortcut args
    handle_deprecated_model_args(args, io)
    if args.openai_api_base:
        os.environ["OPENAI_API_BASE"] = args.openai_api_base
    if args.openai_api_version:
        io.tool_warning(
            "--openai-api-version is deprecated, use --set-env OPENAI_API_VERSION=<value>"
        )
        os.environ["OPENAI_API_VERSION"] = args.openai_api_version
    if args.openai_api_type:
        io.tool_warning("--openai-api-type is deprecated, use --set-env OPENAI_API_TYPE=<value>")
        os.environ["OPENAI_API_TYPE"] = args.openai_api_type
    if args.openai_organization_id:
        io.tool_warning(
            "--openai-organization-id is deprecated, use --set-env OPENAI_ORGANIZATION=<value>"
        )
        os.environ["OPENAI_ORGANIZATION"] = args.openai_organization_id

    analytics = Analytics(
        logfile=args.analytics_log,
        permanently_disable=args.analytics_disable,
        posthog_host=args.analytics_posthog_host,
        posthog_project_api_key=args.analytics_posthog_project_api_key,
    )
    if args.analytics is not False:
        if analytics.need_to_ask(args.analytics):
            io.tool_output(
                "Loom respects your privacy and never collects your code, chat messages, keys or"
                " personal info."
            )
            io.tool_output(f"For more info: {urls.analytics}")
            disable = not io.confirm_ask(
                "Allow collection of anonymous analytics to help improve loom?"
            )

            analytics.asked_opt_in = True
            if disable:
                analytics.disable(permanently=True)
                io.tool_output("Analytics have been permanently disabled.")

            analytics.save_data()
            io.tool_output()

        # This is a no-op if the user has opted out
        analytics.enable()

    analytics.event("launched")

    if args.web and args.gui:
        io.tool_error("--web can't be used with --gui.")
        return 1

    if args.web and not return_coder and not check_web_install(io):
        analytics.event("exit", reason="Web server not installed")
        return 1

    if args.gui and not return_coder:
        if not check_streamlit_install(io):
            analytics.event("exit", reason="Streamlit not installed")
            return
        analytics.event("gui session")
        launch_gui(argv)
        analytics.event("exit", reason="GUI session ended")
        return

    if args.verbose:
        for fname in loaded_dotenvs:
            io.tool_output(f"Loaded {fname}")

    all_files = args.files + (args.file or [])
    fnames = [str(Path(fn).resolve()) for fn in all_files]
    read_only_fnames = []
    for fn in args.read or []:
        path = Path(fn).expanduser().resolve()
        if path.is_dir():
            read_only_fnames.extend(str(f) for f in path.rglob("*") if f.is_file())
        else:
            read_only_fnames.append(str(path))

    if len(all_files) > 1:
        good = True
        for fname in all_files:
            if Path(fname).is_dir():
                io.tool_error(f"{fname} is a directory, not provided alone.")
                good = False
        if not good:
            io.tool_output(
                "Provide either a single directory of a git repo, or a list of one or more files."
            )
            analytics.event("exit", reason="Invalid directory input")
            return 1

    git_dname = None
    if len(all_files) == 1:
        if Path(all_files[0]).is_dir():
            if args.git:
                git_dname = str(Path(all_files[0]).resolve())
                fnames = []
            else:
                io.tool_error(f"{all_files[0]} is a directory, but --no-git selected.")
                analytics.event("exit", reason="Directory with --no-git")
                return 1

    # We can't know the git repo for sure until after parsing the args.
    # If we guessed wrong, reparse because that changes things like
    # the location of the config.yml and history files.
    if args.git and not force_git_root and git is not None:
        right_repo_root = guessed_wrong_repo(io, git_root, fnames, git_dname)
        if right_repo_root:
            analytics.event("exit", reason="Recursing with correct repo")
            return main(argv, input, output, right_repo_root, return_coder=return_coder)

    if args.just_check_update:
        update_available = check_version(io, just_check=True, verbose=args.verbose)
        analytics.event("exit", reason="Just checking update")
        return 0 if not update_available else 1

    if args.install_main_branch:
        success = install_from_main_branch(io)
        analytics.event("exit", reason="Installed main branch")
        return 0 if success else 1

    if args.upgrade:
        success = install_upgrade(io)
        analytics.event("exit", reason="Upgrade completed")
        return 0 if success else 1

    if args.check_update:
        check_version(io, verbose=args.verbose)

    if args.git:
        git_root = setup_git(git_root, io)
        if args.gitignore:
            check_gitignore(git_root, io)

    if args.verbose:
        show = format_settings(parser, args)
        io.tool_output(show)

    cmd_line = " ".join(sys.argv)
    cmd_line = scrub_sensitive_info(args, cmd_line)
    io.tool_output(cmd_line, log_only=True)

    is_first_run = is_first_run_of_new_version(io, verbose=args.verbose)
    check_and_load_imports(io, is_first_run, verbose=args.verbose)

    register_models(git_root, args.model_settings_file, io, verbose=args.verbose)
    register_litellm_models(git_root, args.model_metadata_file, io, verbose=args.verbose)

    if args.list_models:
        models.print_matching_models(io, args.list_models)
        analytics.event("exit", reason="Listed models")
        return 0

    # Process any command line aliases
    if args.alias:
        for alias_def in args.alias:
            # Split on first colon only
            parts = alias_def.split(":", 1)
            if len(parts) != 2:
                io.tool_error(f"Invalid alias format: {alias_def}")
                io.tool_output("Format should be: alias:model-name")
                analytics.event("exit", reason="Invalid alias format error")
                return 1
            alias, model = parts
            models.MODEL_ALIASES[alias.strip()] = model.strip()

    selected_model_name = select_default_model(args, io, analytics)
    if not selected_model_name:
        # Error message and analytics event are handled within select_default_model
        # It might have already offered OAuth if no model/keys were found.
        # If it failed here, we exit.
        return 1
    args.model = selected_model_name  # Update args with the selected model

    # Check if an OpenRouter model was selected/specified but the key is missing
    if args.model.startswith("openrouter/") and not os.environ.get("OPENROUTER_API_KEY"):
        io.tool_warning(
            f"The specified model '{args.model}' requires an OpenRouter API key, which was not"
            " found."
        )
        # Attempt OAuth flow because the specific model needs it
        if offer_openrouter_oauth(io, analytics):
            # OAuth succeeded, the key should now be in os.environ.
            # Check if the key is now present after the flow.
            if os.environ.get("OPENROUTER_API_KEY"):
                io.tool_output(
                    "OpenRouter successfully connected."
                )  # Inform user connection worked
            else:
                # This case should ideally not happen if offer_openrouter_oauth succeeded
                # but check defensively.
                io.tool_error(
                    "OpenRouter authentication seemed successful, but the key is still missing."
                )
                analytics.event(
                    "exit",
                    reason="OpenRouter key missing after successful OAuth for specified model",
                )
                return 1
        else:
            # OAuth failed or was declined by the user
            io.tool_error(
                f"Unable to proceed without an OpenRouter API key for model '{args.model}'."
            )
            io.offer_url(urls.models_and_keys, "Open documentation URL for more info?")
            analytics.event(
                "exit",
                reason="OpenRouter key missing for specified model and OAuth failed/declined",
            )
            return 1

    main_model = models.Model(
        args.model,
        weak_model=args.weak_model,
        editor_model=args.editor_model,
        editor_edit_format=args.editor_edit_format,
        verbose=args.verbose,
    )

    # Check if deprecated remove_reasoning is set
    if main_model.remove_reasoning is not None:
        io.tool_warning(
            "Model setting 'remove_reasoning' is deprecated, please use 'reasoning_tag' instead."
        )

    # Set reasoning effort and thinking tokens if specified
    if args.reasoning_effort is not None:
        # Apply if check is disabled or model explicitly supports it
        if not args.check_model_accepts_settings or (
            main_model.accepts_settings and "reasoning_effort" in main_model.accepts_settings
        ):
            main_model.set_reasoning_effort(args.reasoning_effort)

    if args.thinking_tokens is not None:
        # Apply if check is disabled or model explicitly supports it
        if not args.check_model_accepts_settings or (
            main_model.accepts_settings and "thinking_tokens" in main_model.accepts_settings
        ):
            main_model.set_thinking_tokens(args.thinking_tokens)

    # Show warnings about unsupported settings that are being ignored
    if args.check_model_accepts_settings:
        settings_to_check = [
            {"arg": args.reasoning_effort, "name": "reasoning_effort"},
            {"arg": args.thinking_tokens, "name": "thinking_tokens"},
        ]

        for setting in settings_to_check:
            if setting["arg"] is not None and (
                not main_model.accepts_settings
                or setting["name"] not in main_model.accepts_settings
            ):
                io.tool_warning(
                    f"Warning: {main_model.name} does not support '{setting['name']}', ignoring."
                )
                io.tool_output(
                    f"Use --no-check-model-accepts-settings to force the '{setting['name']}'"
                    " setting."
                )

    if args.copy_paste and args.edit_format is None:
        if main_model.edit_format in ("diff", "whole", "diff-fenced"):
            main_model.edit_format = "editor-" + main_model.edit_format

    if args.verbose:
        io.tool_output("Model metadata:")
        io.tool_output(json.dumps(main_model.info, indent=4))

        io.tool_output("Model settings:")
        for attr in sorted(fields(ModelSettings), key=lambda x: x.name):
            val = getattr(main_model, attr.name)
            val = json.dumps(val, indent=4)
            io.tool_output(f"{attr.name}: {val}")

    approve_project_commands(io, parser, args, project_options, git_root or os.getcwd())
    lint_cmds = parse_lint_cmds(args.lint_cmd, io)
    if lint_cmds is None:
        analytics.event("exit", reason="Invalid lint command format")
        return 1

    if args.show_model_warnings:
        problem = models.sanity_check_models(io, main_model)
        if problem:
            analytics.event("model warning", main_model=main_model)
            io.tool_output("You can skip this check with --no-show-model-warnings")

            try:
                io.offer_url(urls.model_warnings, "Open documentation url for more info?")
                io.tool_output()
            except KeyboardInterrupt:
                analytics.event("exit", reason="Keyboard interrupt during model warnings")
                return 1

    repo = None
    if args.git:
        try:
            repo = GitRepo(
                io,
                fnames,
                git_dname,
                args.loomignore,
                models=main_model.commit_message_models(),
                attribute_author=args.attribute_author,
                attribute_committer=args.attribute_committer,
                attribute_commit_message_author=args.attribute_commit_message_author,
                attribute_commit_message_committer=args.attribute_commit_message_committer,
                commit_prompt=args.commit_prompt,
                subtree_only=args.subtree_only,
                git_commit_verify=args.git_commit_verify,
                attribute_co_authored_by=args.attribute_co_authored_by,  # Pass the arg
            )
        except FileNotFoundError:
            pass

    if not args.skip_sanity_check_repo:
        if not sanity_check_repo(repo, io):
            analytics.event("exit", reason="Repository sanity check failed")
            return 1

    if repo and not args.skip_sanity_check_repo:
        num_files = len(repo.get_tracked_files())
        analytics.event("repo", num_files=num_files)
    else:
        analytics.event("no-repo")

    commands = Commands(
        io,
        None,
        voice_language=args.voice_language,
        voice_input_device=args.voice_input_device,
        voice_format=args.voice_format,
        verify_ssl=args.verify_ssl,
        args=args,
        parser=parser,
        verbose=args.verbose,
        editor=args.editor,
        original_read_only_fnames=read_only_fnames,
    )

    summarizer = ChatSummary(
        [main_model.weak_model, main_model],
        args.max_chat_history_tokens or main_model.max_chat_history_tokens,
    )

    edit_format = args.edit_format
    if edit_format is None and use_agent(args, main_model):
        edit_format = "agent"
    if args.permission_mode == "plan" and edit_format not in ("agent", "ask", "context"):
        # Only the agent asks Permissions; the classic edit formats apply edits as they come
        io.tool_warning(
            "Plan mode needs the agent, which isn't in use, so loom starts in ask mode, where"
            " the model can't change files."
        )
        edit_format = "ask"

    # Caching pays off most for the agent, which resends the growing conversation every step
    if args.cache_prompts is None:
        args.cache_prompts = edit_format == "agent"

    if args.cache_prompts and args.map_refresh == "auto":
        args.map_refresh = "files"

    if not main_model.streaming:
        if args.stream:
            io.tool_warning(
                f"Warning: Streaming is not supported by {main_model.name}. Disabling streaming."
            )
        args.stream = False

    if args.map_tokens is None:
        map_tokens = main_model.get_repo_map_tokens()
    else:
        map_tokens = args.map_tokens

    # Track auto-commits configuration
    analytics.event("auto_commits", enabled=bool(args.auto_commits))

    allow, project_allow = args.allow, []
    if "allow" in project_options:
        # Like the rules in .loom.permissions.json, they need the user's approval
        source = Path(project_options["allow"]).name
        allow, project_allow = [], [(text, source) for text in args.allow]
    io.agent_diffs = args.agent_diffs
    try:
        permissions = Permissions(
            io,
            mode=args.permission_mode,
            allow=allow,
            project_allow=project_allow,
            settings_file=Path(repo.root if repo else Path.cwd()) / SETTINGS_FILE,
            root=Path(repo.root if repo else Path.cwd()),
        )
    except ValueError as err:
        io.tool_error(str(err))
        analytics.event("exit", reason="Invalid permission rule")
        return 1

    project_root = Path(repo.root if repo else Path.cwd())
    session = get_session(args, io, project_root)
    if session is None:
        analytics.event("exit", reason="Unable to resume session")
        return 1
    if session.messages:
        fnames, read_only_fnames = add_session_files(
            session, project_root, fnames, read_only_fnames, io=io
        )

    mcp = None
    if args.mcp:
        try:
            mcp = McpManager.from_config(
                io,
                project_root,
                args.mcp_config,
                project_config_files=args.mcp_config if "mcp_config" in project_options else (),
            )
        except McpError as err:
            io.tool_error(str(err))
            analytics.event("exit", reason="Invalid MCP config")
            return 1
        atexit.register(mcp.close)
        # Only the agent uses MCP tools; another chat mode connects when switching to it
        if edit_format == "agent":
            mcp.start()

    hooks = Hooks.from_config(io, project_root) if args.hooks else None

    try:
        coder = Coder.create(
            main_model=main_model,
            edit_format=edit_format,
            io=io,
            repo=repo,
            fnames=fnames,
            read_only_fnames=read_only_fnames,
            show_diffs=args.show_diffs,
            auto_commits=args.auto_commits,
            dirty_commits=args.dirty_commits,
            dry_run=args.dry_run,
            map_tokens=map_tokens,
            verbose=args.verbose,
            stream=args.stream,
            use_git=args.git,
            restore_chat_history=args.restore_chat_history,
            auto_lint=args.auto_lint,
            auto_test=args.auto_test,
            lint_cmds=lint_cmds,
            test_cmd=args.test_cmd,
            commands=commands,
            summarizer=summarizer,
            analytics=analytics,
            map_refresh=args.map_refresh,
            cache_prompts=args.cache_prompts,
            map_mul_no_files=args.map_multiplier_no_files,
            num_cache_warming_pings=args.cache_keepalive_pings,
            suggest_shell_commands=args.suggest_shell_commands,
            chat_language=args.chat_language,
            commit_language=args.commit_language,
            detect_urls=args.detect_urls,
            auto_copy_context=args.copy_paste,
            auto_accept_architect=args.auto_accept_architect,
            add_gitignore_files=args.add_gitignore_files,
            permissions=permissions,
            project_memory=args.project_memory,
            session=session,
            done_messages=session.messages or None,
            auto_compact=args.auto_compact,
            project_settings=dict(
                build_retries=max(0, args.build_retries),
                build_budget=args.build_budget,
                build_workers=max(1, args.build_workers),
                allow_deploy=args.allow_deploy,
            ),
            mcp=mcp,
            hooks=hooks,
            checkpoint_steps=args.checkpoint_steps,
            web_tools=args.web_tools,
            web_search=args.web_search,
            subagent_settings=dict(
                enabled=args.subagents,
                max_steps=max(1, args.subagent_max_steps),
                budget=args.subagent_budget,
                models=args.subagent_model,
            ),
        )
    except UnknownEditFormat as err:
        io.tool_error(str(err))
        io.offer_url(urls.edit_formats, "Open documentation about edit formats?")
        analytics.event("exit", reason="Unknown edit format")
        return 1
    except ValueError as err:
        io.tool_error(str(err))
        analytics.event("exit", reason="ValueError during coder creation")
        return 1

    if return_coder:
        analytics.event("exit", reason="Returning coder object")
        return coder

    ignores = []
    if git_root:
        ignores.append(str(Path(git_root) / ".gitignore"))
    if args.loomignore:
        ignores.append(args.loomignore)

    if args.watch_files:
        file_watcher = FileWatcher(
            coder,
            gitignores=ignores,
            verbose=args.verbose,
            analytics=analytics,
            root=str(Path.cwd()) if args.subtree_only else None,
        )
        coder.file_watcher = file_watcher

    if args.copy_paste:
        analytics.event("copy-paste mode")
        ClipboardWatcher(coder.io, verbose=args.verbose)

    coder.show_announcements()
    if session.messages:
        coder.show_session_recap()

    if args.show_prompts:
        coder.cur_messages += [
            dict(role="user", content="Hello!"),
        ]
        messages = coder.format_messages().all_messages()
        utils.show_messages(messages)
        analytics.event("exit", reason="Showed prompts")
        return

    if args.lint:
        coder.commands.cmd_lint(fnames=fnames)

    if args.test:
        if not args.test_cmd:
            io.tool_error("No --test-cmd provided.")
            analytics.event("exit", reason="No test command provided")
            return 1
        coder.commands.cmd_test(args.test_cmd)
        if io.placeholder:
            coder.run(io.placeholder)

    if args.commit:
        if args.dry_run:
            io.tool_output("Dry run enabled, skipping commit.")
        else:
            coder.commands.cmd_commit()

    if args.lint or args.test or args.commit:
        analytics.event("exit", reason="Completed lint/test/commit")
        return

    if args.show_repo_map:
        repo_map = coder.get_repo_map()
        if repo_map:
            io.tool_output(repo_map)
        analytics.event("exit", reason="Showed repo map")
        return

    if args.apply:
        content = io.read_text(args.apply)
        if content is None:
            analytics.event("exit", reason="Failed to read apply content")
            return
        coder.partial_response_content = content
        # For testing #2879
        # from loom.coders.base_coder import all_fences
        # coder.fence = all_fences[1]
        coder.apply_updates()
        analytics.event("exit", reason="Applied updates")
        return

    if args.apply_clipboard_edits:
        args.edit_format = main_model.editor_edit_format
        args.message = "/paste"

    if args.show_release_notes is True and urls.release_notes:
        io.tool_output(f"Opening release notes: {urls.release_notes}")
        io.tool_output()
        webbrowser.open(urls.release_notes)
    elif args.show_release_notes is None and is_first_run:
        io.tool_output()
        io.offer_url(
            urls.release_notes,
            "Would you like to see what's new in this version?",
            allow_never=False,
        )

    if git_root and Path.cwd().resolve() != Path(git_root).resolve():
        io.tool_warning(
            "Note: in-chat filenames are always relative to the git working dir, not the current"
            " working dir."
        )

        io.tool_output(f"Cur working dir: {Path.cwd()}")
        io.tool_output(f"Git working dir: {git_root}")

    if args.load:
        commands.cmd_load(args.load)

    if args.message or args.message_file:
        # Nothing can approve a plan made in plan mode: it's shown, and loom exits
        coder.one_shot = True

    if args.message:
        io.add_to_input_history(args.message)
        io.tool_output()
        try:
            coder.run(with_message=args.message)
        except SwitchCoder:
            pass
        analytics.event("exit", reason="Completed --message")
        return

    if args.message_file:
        try:
            message_from_file = io.read_text(args.message_file)
            io.tool_output()
            coder.run(with_message=message_from_file)
        except FileNotFoundError:
            io.tool_error(f"Message file not found: {args.message_file}")
            analytics.event("exit", reason="Message file not found")
            return 1
        except IOError as e:
            io.tool_error(f"Error reading message file: {e}")
            analytics.event("exit", reason="Message file IO error")
            return 1

        analytics.event("exit", reason="Completed --message-file")
        return

    if args.exit:
        analytics.event("exit", reason="Exit flag set")
        return

    if args.web:
        if not start_web(io, args):
            analytics.event("exit", reason="Web server didn't start")
            return 1
        analytics.event("web session", main_model=main_model, edit_format=main_model.edit_format)
    else:
        analytics.event("cli session", main_model=main_model, edit_format=main_model.edit_format)

    while True:
        try:
            coder.ok_to_warm_cache = bool(args.cache_keepalive_pings)
            coder.run()
            analytics.event("exit", reason="Completed main CLI coder.run")
            return
        except SwitchCoder as switch:
            coder.ok_to_warm_cache = False

            # Set the placeholder if provided
            if hasattr(switch, "placeholder") and switch.placeholder is not None:
                io.placeholder = switch.placeholder

            kwargs = dict(io=io, from_coder=coder)
            kwargs.update(switch.kwargs)
            if "show_announcements" in kwargs:
                del kwargs["show_announcements"]

            coder = Coder.create(**kwargs)

            if switch.kwargs.get("show_announcements") is not False:
                coder.show_announcements()


def get_session(args, io, root):
    """The conversation to use: a saved one for --continue or --resume, otherwise a new one.
    Returns None if --resume names a session that can't be loaded."""
    directory = Path(root) / SESSIONS_DIR if args.sessions else None

    if args.resume:
        if not directory:
            io.tool_error("--resume can't be used with --no-sessions.")
            return None
        try:
            return Session.find(directory, args.resume)
        except SessionError as err:
            io.tool_error(str(err))
            return None

    if args.continue_session:
        session = Session.latest(directory) if directory else None
        if session:
            return session
        io.tool_warning("There's no saved conversation to continue here, so starting a new one.")

    return Session(directory)


def add_session_files(session, root, fnames, read_only_fnames, io=None):
    """Add the files that were in the chat when the session was saved, if they still exist.

    A session file comes with the repo on \'loom --resume\', so a crafted one could list
    files outside the project (\'files: [\'../../home/.ssh/id_rsa\']\'). Entries that
    aren\'t inside the project are dropped with a warning; the user can add such a file
    with /read if they really meant to."""
    fnames = list(fnames)
    read_only_fnames = list(read_only_fnames)
    root_resolved = safe_resolve(root)

    def inside(path):
        try:
            resolved = Path(path).resolve()
        except (OSError, RuntimeError):
            return False
        return resolved == root_resolved or root_resolved in resolved.parents

    skipped = []
    for rel_fname in session.data.get("files") or []:
        if not isinstance(rel_fname, str):
            continue
        try:
            path = (Path(root) / rel_fname).resolve()
        except (OSError, RuntimeError):
            skipped.append(rel_fname)
            continue
        if not inside(path):
            skipped.append(rel_fname)
            continue
        if path.is_file() and str(path) not in fnames:
            fnames.append(str(path))
    for fname in session.data.get("read_only_files") or []:
        if not isinstance(fname, str):
            continue
        if not inside(fname):
            skipped.append(fname)
            continue
        path = Path(fname)
        if path.is_file() and str(path) not in read_only_fnames:
            read_only_fnames.append(str(path))
    if skipped and io is not None:
        io.tool_warning(
            "Skipped session files that aren't inside this project: "
            + ", ".join(sorted(set(skipped))[:10])
            + ". Add them with /read or /add if you need them."
        )
    return fnames, read_only_fnames


def use_agent(args, main_model):
    """Whether to default to the agent, when no edit format was chosen."""
    if not args.agent or args.copy_paste or args.apply or args.apply_clipboard_edits or args.gui:
        return False
    return main_model.info.get("supports_function_calling") is True


def is_first_run_of_new_version(io, verbose=False):
    """Check if this is the first run of a new version/executable combination"""
    installs_file = Path.home() / ".loom" / "installs.json"
    key = (__version__, sys.executable)

    # Never show notes for .dev versions
    if ".dev" in __version__:
        return False

    if verbose:
        io.tool_output(
            f"Checking imports for version {__version__} and executable {sys.executable}"
        )
        io.tool_output(f"Installs file: {installs_file}")

    try:
        if installs_file.exists():
            with open(installs_file, "r") as f:
                installs = json.load(f)
            if verbose:
                io.tool_output("Installs file exists and loaded")
        else:
            installs = {}
            if verbose:
                io.tool_output("Installs file does not exist, creating new dictionary")

        is_first_run = str(key) not in installs

        if is_first_run:
            installs[str(key)] = True
            installs_file.parent.mkdir(parents=True, exist_ok=True)
            with open(installs_file, "w") as f:
                json.dump(installs, f, indent=4)

        return is_first_run

    except Exception as e:
        io.tool_warning(f"Error checking version: {e}")
        if verbose:
            io.tool_output(f"Full exception details: {traceback.format_exc()}")
        return True  # Safer to assume it's a first run if we hit an error


def check_and_load_imports(io, is_first_run, verbose=False):
    try:
        if is_first_run:
            if verbose:
                io.tool_output(
                    "First run for this version and executable, loading imports synchronously"
                )
            try:
                load_slow_imports(swallow=False)
            except Exception as err:
                io.tool_error(str(err))
                io.tool_output("Error loading required imports. Did you install loom properly?")
                io.offer_url(urls.install_properly, "Open documentation url for more info?")
                sys.exit(1)

            if verbose:
                io.tool_output("Imports loaded and installs file updated")
        else:
            if verbose:
                io.tool_output("Not first run, loading imports in background thread")
            thread = threading.Thread(target=load_slow_imports)
            thread.daemon = True
            thread.start()

    except Exception as e:
        io.tool_warning(f"Error in loading imports: {e}")
        if verbose:
            io.tool_output(f"Full exception details: {traceback.format_exc()}")


def load_slow_imports(swallow=True):
    # These imports are deferred in various ways to
    # improve startup time.
    # This func is called either synchronously or in a thread
    # depending on whether it's been run before for this version and executable.

    try:
        import httpx  # noqa: F401
        import litellm  # noqa: F401
        import networkx  # noqa: F401
        import numpy  # noqa: F401
    except Exception as e:
        if not swallow:
            raise e


if __name__ == "__main__":
    status = main()
    sys.exit(status)
