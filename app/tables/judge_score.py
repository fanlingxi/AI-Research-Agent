"""Score two complete blinded judges conservatively, pending arbitration."""

import argparse
from pathlib import Path

from .io import read, write
from .score import proportion


def score(manifest, judges):
    if len(judges) != 2 or judges[0].resolve() == judges[1].resolve():
        raise ValueError("Two distinct judge directories required")
    packets = read(manifest)["packets"]
    expected = {p["packet_id"] for p in packets}
    for directory in judges:
        if {p.stem for p in directory.glob("*.json")} != expected:
            raise ValueError("Each judge must answer every packet exactly once")
    total = correct = whole = 0
    merged = []
    for packet in packets:
        key = packet["packet_id"]
        coordinates = {(c["row"], c["col"]) for c in packet["cells"]}
        wrong = set()
        table_correct = True
        errors = []
        for directory in judges:
            answer = read(directory / f"{key}.json")
            if answer["packet_id"] != key or type(answer["table_correct"]) is not bool:
                raise ValueError("Invalid packet answer")
            table_correct &= answer["table_correct"]
            for error in answer["cell_errors"]:
                if error["kind"] not in ("text", "position", "span", "missing", "extra"):
                    raise ValueError("Invalid error kind")
                coordinate = (error["row"], error["col"])
                if any(type(v) is not int or v < 0 for v in coordinate):
                    raise ValueError("Invalid cell coordinate")
                if coordinate not in coordinates and error["kind"] not in ("missing", "extra"):
                    raise ValueError("Unknown claimed cell")
                wrong.add(coordinate)
                errors.append({"judge": directory.name, **error})
            if answer["table_correct"] and answer["cell_errors"]:
                raise ValueError("Contradictory table_correct")
        # Extra/missing coordinates outside the claim add an incorrect cell to the denominator.
        denominator = coordinates | wrong
        total += len(denominator)
        correct += len(coordinates - wrong)
        whole += table_correct and not wrong
        merged.append(
            {
                "packet_id": key,
                "cell_errors": errors,
                "wrong_cells": sorted(wrong),
                "table_correct": table_correct and not wrong,
            }
        )
    return {
        "cell_accuracy": proportion(correct, total),
        "whole_table_correct_rate": proportion(whole, len(packets)),
        "pending_arbitration": True,
        "merged": merged,
        "interval": "95% Wilson; cell independence is descriptive only",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--judges", type=Path, nargs=2, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    write(args.output, score(args.manifest, args.judges))


if __name__ == "__main__":
    main()
