from __future__ import annotations

from stockquant.data.demo import write_demo_data


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Generate synthetic market data")
    parser.add_argument("--output", required=True)
    parser.add_argument("--start", default="2022-01-03")
    parser.add_argument("--end", default="2025-12-31")
    parser.add_argument("--seed", type=int, default=20261003)
    args = parser.parse_args()
    rows = write_demo_data(args.output, start=args.start, end=args.end, seed=args.seed)
    print(f"wrote {len(rows)} rows to {args.output}")


if __name__ == "__main__":
    main()
