import os
import sys
import time
from pathlib import Path

import packaging.version

import loom
from loom import utils
from loom.dump import dump  # noqa: F401

VERSION_CHECK_FNAME = Path.home() / ".loom" / "caches" / "versioncheck"
RELEASES_URL = "https://api.github.com/repos/sri-venkat-22/loom/releases/latest"


def install_from_main_branch(io):
    """
    Install the latest version of loom from the main branch of the GitHub repository.
    """

    return utils.check_pip_install_extra(
        io,
        None,
        "Install the development version of loom from the main branch?",
        [utils.LOOM_GIT_URL],
        self_update=True,
    )


def install_upgrade(io, latest_version=None):
    """
    Install the latest tagged release of loom from GitHub.
    """

    if latest_version:
        new_ver_text = f"Newer loom version v{latest_version} is available."
    else:
        new_ver_text = "Install latest version of loom?"

    docker_image = os.environ.get("LOOM_DOCKER_IMAGE")
    if docker_image:
        text = f"""
{new_ver_text} To upgrade, run:

    docker pull {docker_image}
"""
        io.tool_warning(text)
        return True

    success = utils.check_pip_install_extra(
        io,
        None,
        new_ver_text,
        [f"{utils.LOOM_GIT_URL}@v{latest_version}" if latest_version else utils.LOOM_GIT_URL],
        self_update=True,
    )

    if success:
        io.tool_output("Re-run loom to use new version.")
        sys.exit()

    return


def check_version(io, just_check=False, verbose=False):
    if not just_check and VERSION_CHECK_FNAME.exists():
        day = 60 * 60 * 24
        since = time.time() - os.path.getmtime(VERSION_CHECK_FNAME)
        if 0 < since < day:
            if verbose:
                hours = since / 60 / 60
                io.tool_output(f"Too soon to check version: {hours:.1f} hours")
            return

    # To keep startup fast, avoid importing this unless needed
    import requests

    try:
        # loom is released as GitHub tags (vX.Y.Z), not on PyPI.
        response = requests.get(RELEASES_URL, timeout=10)
        if response.status_code == 404:
            latest_version = "0"  # no releases published yet
        else:
            response.raise_for_status()
            latest_version = response.json()["tag_name"].lstrip("v")
        current_version = loom.__version__

        if just_check or verbose:
            io.tool_output(f"Current version: {current_version}")
            io.tool_output(f"Latest version: {latest_version}")

        is_update_available = packaging.version.parse(latest_version) > packaging.version.parse(
            current_version
        )
    except Exception as err:
        io.tool_error(f"Error checking GitHub for new version: {err}")
        return False
    finally:
        VERSION_CHECK_FNAME.parent.mkdir(parents=True, exist_ok=True)
        VERSION_CHECK_FNAME.touch()

    ###
    # is_update_available = True

    if just_check or verbose:
        if is_update_available:
            io.tool_output("Update available")
        else:
            io.tool_output("No update available")

    if just_check:
        return is_update_available

    if not is_update_available:
        return False

    install_upgrade(io, latest_version)
    return True
