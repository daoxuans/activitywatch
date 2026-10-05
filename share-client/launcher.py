"""PyInstaller entry point for the visible, opt-in ActivityWatch share client."""

from aw_share.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
