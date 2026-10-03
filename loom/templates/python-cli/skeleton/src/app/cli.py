"""The command-line interface: parse the arguments, then call the package's functions."""

import argparse
import sys

from app import __version__


def build_parser():
    parser = argparse.ArgumentParser(prog="app", description="A command-line tool.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    hello = commands.add_parser("hello", help="say hello")
    hello.add_argument("name", nargs="?", default="world", help="who to greet")
    return parser


def greeting(name):
    return f"Hello, {name}!"


def main(argv=None):
    """Run the tool with argv (sys.argv[1:] by default) and return its exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "hello":
        print(greeting(args.name))
        return 0
    parser.print_help(sys.stderr)
    return 2
