import os
import unittest
from unittest.mock import patch

from loom.main import main


class TestBrowser(unittest.TestCase):
    @patch("loom.main.launch_gui")
    def test_browser_flag_imports_streamlit(self, mock_launch_gui):
        os.environ["LOOM_ANALYTICS"] = "false"

        # Run main with --gui (--browser now opens the --web UI instead) and --yes
        main(["--gui", "--yes"])

        # Check that launch_gui was called
        mock_launch_gui.assert_called_once()

        # Try to import streamlit
        try:
            import streamlit  # noqa: F401

            streamlit_imported = True
        except ImportError:
            streamlit_imported = False

        # Assert that streamlit was successfully imported
        self.assertTrue(
            streamlit_imported, "Streamlit should be importable after running with --gui flag"
        )


if __name__ == "__main__":
    unittest.main()
