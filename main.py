import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
SRC_DIR = PROJECT_ROOT / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from competitor_tracker.cli import main as competitor_tracker_main
from indrive_media.main import main as legacy_indrive_media_main


LEGACY_COMMANDS = {"legacy-indrive-media", "legacy", "indrive-media"}


def main(argv: list[str] | None = None) -> None:
    """Route the repository root entrypoint to the MVP by default."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in LEGACY_COMMANDS:
        sys.argv = [sys.argv[0], *args[1:]]
        legacy_indrive_media_main()
        return
    competitor_tracker_main(args)


if __name__ == "__main__":
    main()
