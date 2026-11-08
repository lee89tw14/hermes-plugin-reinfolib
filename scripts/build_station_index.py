"""Rebuild reinfolib/data/n02-stations.json from the official N02 ZIP (developer tool).

Usage:
  python scripts/build_station_index.py N02-25_GML.zip --dataset N02-25 --sha256 <hex>

Download the ZIP yourself from the official page (国土数値情報 鉄道データ) and pass the
SHA-256 you recorded; the build refuses any other file. No network access here.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reinfolib.stations import INDEX_PATH, StationIndexError, build_index, dumps_index  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('zip_path', type=Path)
    parser.add_argument('--dataset', required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--out', type=Path, default=INDEX_PATH)
    args = parser.parse_args(argv)
    try:
        index = build_index(args.zip_path.read_bytes(), dataset=args.dataset, expected_sha256=args.sha256)
    except StationIndexError as error:
        print(f'error: {error.error_type}/{error.detail}', file=sys.stderr)
        return 1
    args.out.write_bytes(dumps_index(index))
    print(f"wrote {args.out.name}: {index['station_count']} stations, {index['group_count']} groups, "
          f"dataset {index['dataset']}, zip sha256 {index['source_zip_sha256']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
