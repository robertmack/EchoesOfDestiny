import argparse

from remembering.game import Game


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Remembering.")
    parser.add_argument(
        "--skip-intro",
        action="store_true",
        help="start directly in the game without playing the opening images",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    Game(skip_intro=args.skip_intro).run()


if __name__ == "__main__":
    main()
