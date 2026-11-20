"""Public entry point that never uses binary completion packages."""

from converter_console import main


if __name__ == '__main__':
    raise SystemExit(main(public=True))
