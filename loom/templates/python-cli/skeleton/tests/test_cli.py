import pytest
from app import __version__
from app.cli import greeting, main


def test_hello(capsys):
    assert main(["hello", "loom"]) == 0
    assert capsys.readouterr().out == "Hello, loom!\n"


def test_hello_has_a_default():
    assert greeting("world") == "Hello, world!"
    assert main(["hello"]) == 0


def test_version(capsys):
    with pytest.raises(SystemExit) as exit:
        main(["--version"])
    assert exit.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_command_shows_the_help(capsys):
    assert main([]) == 2
    assert "usage:" in capsys.readouterr().err


def test_bad_arguments(capsys):
    with pytest.raises(SystemExit) as exit:
        main(["frobnicate"])
    assert exit.value.code == 2
