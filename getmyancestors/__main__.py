def main() -> int:
    """Run the package CLI entry point."""
    from getmyancestors.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
