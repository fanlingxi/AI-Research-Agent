"""Glyph-level engine topology votes; positions and text come only from glyphs."""

from copy import deepcopy
from difflib import SequenceMatcher
from functools import lru_cache

from .chars import compact, text_lines
from .geometry import contains
from .verify import verify

SIMILARITY = 0.65


def topology(grid):
    return sorted((c["row"], c["col"], c["rowspan"], c["colspan"]) for c in grid["cells"])


def vote(physical, logical, engine):
    cells = deepcopy(logical)
    result = dict(
        engine=engine,
        cells=[],
        errors=[],
        vote_passed=False,
        agreement_score=0.0,
        glyph_level_vote=True,
    )
    if not cells:
        result["errors"] = ["Empty engine topology"]
        return result
    nr = max(c["row"] + c["rowspan"] for c in cells)
    nc = max(c["col"] + c["colspan"] for c in cells)
    lines, ys = physical["lines"], physical["ys"]
    slots = [
        (r, k)
        for c in cells
        for r in range(c["row"], c["row"] + c["rowspan"])
        for k in range(c["col"], c["col"] + c["colspan"])
    ]
    if len(slots) != nr * nc or len(set(slots)) != len(slots) or nr > len(lines):
        result["errors"] = ["Engine topology cannot cover source lines"]
        return result
    low, high = physical["xs"][0], physical["xs"][-1]

    def align_row(row, mapping, matched):
        active = sorted(
            (i for i, c in enumerate(cells) if c["row"] <= row < c["row"] + c["rowspan"]),
            key=lambda i: cells[i]["col"],
        )
        stop = max(mapping[cells[i]["row"] + cells[i]["rowspan"]] for i in active)
        glyphs = [g for line in lines[mapping[row] : stop] for g in line]
        endpoints = sorted(
            {low, high, *(v for g in glyphs for v in (g["bbox_pt"][0], g["bbox_pt"][2]))}
        )
        cuts = sorted(
            {
                low,
                high,
                *(
                    (a + b) / 2
                    for a, b in zip(endpoints, endpoints[1:], strict=False)
                    if not any(g["bbox_pt"][0] < (a + b) / 2 < g["bbox_pt"][2] for g in glyphs)
                ),
                *(
                    v
                    for i in active
                    if i in matched
                    for v in (matched[i]["bbox_pt"][0], matched[i]["bbox_pt"][2])
                ),
            }
        )

        @lru_cache(None)
        def dp(pos, left):
            if pos == len(active):
                return (0, ()) if left == high else None
            i = active[pos]
            c = cells[i]
            if i in matched:
                box = matched[i]["bbox_pt"]
                if abs(left - box[0]) > 1e-6:
                    return None
                rest = dp(pos + 1, box[2])
                return rest
            best = None
            end = mapping[c["row"] + c["rowspan"]]
            local = [g for line in lines[mapping[row] : end] for g in line]
            for right in cuts:
                if right <= left or (pos == len(active) - 1 and right != high):
                    continue
                box = [left, ys[mapping[row]], right, ys[end]]
                owned = [g for g in local if contains(box, g["bbox_pt"])]
                if any(
                    g["bbox_pt"][0] < right < g["bbox_pt"][2]
                    or g["bbox_pt"][0] < left < g["bbox_pt"][2]
                    for g in local
                ):
                    continue
                grouped = text_lines(owned, segments=False)
                text = "\n".join("".join(g["text"] for g in line) for line in grouped)
                a, b = compact(c.get("model_text", "")).casefold(), compact(text).casefold()
                sim = SequenceMatcher(None, a, b, autojunk=False).ratio()
                numeric = sum(
                    any(any(t.isdigit() for t in g["text"]) for g in line) for line in grouped
                )
                # Wrapping labels is permitted; stacked numeric values are a
                # body-row merge. Do not hide extra body numbers in a text header.
                merged = row > 0 and c["col"] > 0 and numeric > 1
                if (
                    bool(a) != bool(b)
                    or sim < SIMILARITY
                    or merged
                    or (any(t.isdigit() for t in b) and not any(t.isdigit() for t in a))
                ):
                    continue
                rest = dp(pos + 1, right)
                if rest is not None:
                    cell = dict(
                        c, bbox_pt=box, text=text, vote_cell_passed=True, vote_cell_agreement=sim
                    )
                    choice = (sim + rest[0], ((i, cell),) + rest[1])
                    if best is None or choice[0] > best[0]:
                        best = choice
            return best

        return dp(0, low)

    best = None
    best_score = -1
    frontier = {}

    def search(mapping, matched, done, score):
        nonlocal best, best_score
        boundary = len(mapping) - 1
        additions = dict(matched)
        completed = set(done)
        for row in range(boundary):
            if row in completed:
                continue
            active = [c for c in cells if c["row"] <= row < c["row"] + c["rowspan"]]
            if any(c["row"] + c["rowspan"] > boundary for c in active):
                continue
            aligned = align_row(row, mapping, additions)
            if aligned is None:
                return
            score += aligned[0]
            additions.update(aligned[1])
            completed.add(row)
        # Row-boundary DP retains pending rowspan starts and their fixed x ranges.
        # Completed rows have no influence on future whitespace/text assignments.
        pending = min((r for r in range(boundary) if r not in completed), default=boundary)
        continuing = tuple(
            (i, tuple(c["bbox_pt"]))
            for i, c in sorted(additions.items())
            if cells[i]["row"] + cells[i]["rowspan"] > pending
        )
        state = (boundary, tuple(mapping[pending:]), continuing)
        if frontier.get(state, -1) >= score:
            return
        frontier[state] = score
        if boundary == nr:
            owned = [
                g["id"]
                for c in additions.values()
                for g in physical["chars"]
                if contains(c["bbox_pt"], g["bbox_pt"])
            ]
            if sorted(owned) == sorted(g["id"] for g in physical["chars"]) and score > best_score:
                best, best_score = additions, score
            return
        for end in range(mapping[-1] + 1, len(lines) - (nr - boundary - 1) + 1):
            if any(mapping[-1] < h < end for h in physical["hard_rows"]):
                break
            search(mapping + [end], additions, completed, score)

    search([0], {}, set(), 0)
    if best is None:
        result["errors"] = ["No complete monotone glyph agreement"]
    else:
        result.update(
            cells=[best[i] for i in range(len(cells))],
            vote_passed=True,
            agreement_score=best_score / len(cells),
            vote_topology=topology({"cells": cells}),
        )
    return result


def consensus(page, candidate, physical, engines):
    votes = {name: vote(physical, cells, name) for name, cells in engines.items()}
    for v in votes.values():
        v["verification_result"] = verify(page, candidate, v)
    from .final import final_status

    result = final_status(page, candidate, votes)
    return {**result, "votes": votes}
