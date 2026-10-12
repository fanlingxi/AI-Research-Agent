"""Human review of evaluation archives, isolated from the knowledge data domain."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from app.experiments.service import ExperimentDataError, ExperimentReadService


class ReviewInputError(ValueError):
    pass


class HumanVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    covered: dict[str, StrictBool]
    violated: list[StrictBool]
    outcome_ok: StrictBool
    note: str = Field(default="", max_length=4000)
    judge: Literal["human"] = "human"
    corrected_note: str | None = Field(default=None, max_length=4000)


class Conclusion(BaseModel):
    id: str
    statement: str


class Packet(BaseModel):
    question: str
    expected_outcome: str
    outcome_guide: dict[str, str]
    required_conclusions: list[Conclusion]
    forbidden_conclusions: list[str]
    missing_information: list[str] = Field(default_factory=list)
    report: str
    instructions: str = ""


def _component(value: str) -> str:
    # Restrict Windows device paths, ADS, separators, traversal and ambiguous names too.
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", value)
        or ".." in value
        or value.endswith(".")
        or value.split(".")[0].upper() in {
            "CON", "PRN", "AUX", "NUL",
            *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10)),
        }
    ):
        raise ReviewInputError("评测路径标识无效。")
    return value


def _reference(relative: str) -> str:
    return base64.urlsafe_b64encode(relative.encode()).decode().rstrip("=")


class ReviewService:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def _safe(self, path: Path) -> Path:
        # Reject aliases even within root: human writes must never reach another judge/run.
        if not path.is_relative_to(self.root) or path.resolve() != path.absolute():
            raise ReviewInputError("评测路径越界或包含链接。")
        if not path.resolve().is_relative_to(self.root):
            raise ReviewInputError("评测路径越界。")
        return path

    def _run(self, run_ref: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,2000}", run_ref):
            raise ReviewInputError("运行标识无效。")
        try:
            relative = base64.b64decode(
                run_ref + "=" * (-len(run_ref) % 4), altchars=b"-_", validate=True,
            ).decode("utf-8")
        except (ValueError, UnicodeError, binascii.Error) as exc:
            raise ReviewInputError("运行标识无效。") from exc
        if _reference(relative) != run_ref:
            raise ReviewInputError("运行标识无效。")
        parts = [_component(part) for part in relative.split("/")]
        directory = self._safe(self.root.joinpath(*parts))
        if not self._safe(directory / "judge-packets").is_dir():
            raise KeyError(run_ref)
        return directory

    def _read(self, path: Path):
        return ExperimentReadService._read(self.root, self._safe(path))

    def _packet(self, directory: Path, key: str) -> dict:
        path = self._safe(directory / "judge-packets" / f"{_component(key)}.json")
        if not path.is_file():
            raise KeyError(key)
        try:
            packet = Packet.model_validate(self._read(path)).model_dump()
        except ValidationError as exc:
            raise ExperimentDataError("核验题目格式无效。") from exc
        ids = [item["id"] for item in packet["required_conclusions"]]
        if len(ids) != len(set(ids)):
            raise ExperimentDataError("必答结论标识重复。")
        return packet

    @staticmethod
    def _validate_verdict(verdict: dict, packet: dict):
        ids = {item["id"] for item in packet["required_conclusions"]}
        if (
            not isinstance(verdict, dict)
            or not isinstance(verdict.get("covered"), dict)
            or set(verdict["covered"]) != ids
            or any(type(value) is not bool for value in verdict["covered"].values())
            or not isinstance(verdict.get("violated"), list)
            or len(verdict["violated"]) != len(packet["forbidden_conclusions"])
            or any(type(value) is not bool for value in verdict["violated"])
            or type(verdict.get("outcome_ok")) is not bool
        ):
            raise ReviewInputError("结论标识、禁止结论数量或判断值与题目不匹配。")

    def _judges(self, directory: Path) -> list[Path]:
        return [self._safe(folder) for folder in sorted(directory.glob("judge-*"))
                if folder.name not in {"judge-packets", "judge-human"} and folder.is_dir()]

    def _verdict(self, path: Path, packet: dict):
        if not self._safe(path).is_file():
            return None
        value = self._read(path)
        try:
            self._validate_verdict(value, packet)
        except ReviewInputError as exc:
            raise ExperimentDataError("裁判记录格式无效，请检查归档。") from exc
        return {key: value[key] for key in
                ("covered", "violated", "outcome_ok", "note", "judge", "corrected_note")
                if key in value}

    def _records(self, directory: Path, key: str, packet: dict):
        human = self._verdict(directory / "judge-human" / f"{key}.json", packet)
        judges = {}
        for folder in self._judges(directory):
            value = self._verdict(folder / f"{key}.json", packet)
            if value is not None:
                judges[folder.name.removeprefix("judge-")] = value
        return human, judges

    def _keys(self, directory: Path) -> list[str]:
        return [_component(path.stem) for path in
                sorted(self._safe(directory / "judge-packets").glob("*.json"))
                if self._safe(path).is_file()]

    def items(self, run_ref: str):
        directory = self._run(run_ref)
        items = []
        for key in self._keys(directory):
            packet = self._packet(directory, key)
            human, judges = self._records(directory, key, packet)
            signatures = {json.dumps([v["covered"], v["violated"], v["outcome_ok"]],
                                     sort_keys=True) for v in judges.values()}
            items.append({"key": key, "human_reviewed": human is not None,
                          "ai_agree": len(signatures) == 1 if len(judges) >= 2 else None,
                          "ai_judge_count": len(judges)})
        items.sort(key=lambda item: (
            item["ai_agree"] is not False, item["human_reviewed"], item["key"],
        ))
        return {"items": items, "total": len(items),
                "human_count": sum(item["human_reviewed"] for item in items)}

    def runs(self):
        runs = []
        # os.walk does not follow symlinks; prune resolved aliases/junctions before descent.
        for current, children, _ in os.walk(self.root, followlinks=False):
            directory = Path(current)
            children[:] = sorted(name for name in children
                                 if (directory / name).resolve() == (directory / name).absolute())
            if "judge-packets" not in children:
                continue
            relative = directory.relative_to(self.root).as_posix()
            run_ref = _reference(relative)
            catalog = self.items(run_ref)
            runs.append({"run_ref": run_ref, "dataset": relative.split("/")[0],
                         "name": directory.name, "item_count": catalog["total"],
                         "question_count": len({i["key"].split("__")[0]
                                                for i in catalog["items"]}),
                         "human_count": catalog["human_count"],
                         "ai_judge_count": len(self._judges(directory))})
            children[:] = [name for name in children if not name.startswith("judge-")]
        return {"runs": runs}

    def item(self, run_ref: str, key: str):
        directory = self._run(run_ref)
        packet = self._packet(directory, key)
        human, judges = self._records(directory, key, packet)
        result = {"key": key, "packet": packet, "human": human}
        if human is not None:
            result["ai_judges"] = judges
        return result

    def save(self, run_ref: str, key: str, verdict: HumanVerdict):
        directory = self._run(run_ref)
        packet = self._packet(directory, key)
        value = verdict.model_dump(exclude_none=True)
        self._validate_verdict(value, packet)
        folder = self._safe(directory / "judge-human")
        target = self._safe(folder / f"{key}.json")
        folder.mkdir(exist_ok=True)
        # Atomic replacement keeps concurrent readers from observing partial JSON, and
        # replaces any existing hard link rather than modifying its other target.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder,
                                             suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(value, stream, ensure_ascii=False, indent=2)
            self._safe(target)
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return {"key": key, "human": value}

    def agreement(self, run_ref: str):
        directory = self._run(run_ref)
        scores = {folder.name.removeprefix("judge-"): {
            "paired_count": 0, "outcome_ok": [], "covered": [], "violated": [],
            "covered_by_id": {},
        } for folder in self._judges(directory)}
        human_count = 0
        for key in self._keys(directory):
            packet = self._packet(directory, key)
            human, judges = self._records(directory, key, packet)
            if human is None:
                continue
            human_count += 1
            for name, ai in judges.items():
                score = scores[name]
                score["paired_count"] += 1
                score["outcome_ok"].append(human["outcome_ok"] == ai["outcome_ok"])
                for cid, value in human["covered"].items():
                    agree = value == ai["covered"][cid]
                    score["covered"].append(agree)
                    score["covered_by_id"].setdefault(cid, []).append(agree)
                score["violated"].extend(a == b for a, b in
                                         zip(human["violated"], ai["violated"], strict=True))

        def metric(values):
            return {"matched": sum(values), "total": len(values),
                    "rate": sum(values) / len(values) if values else None}

        return {"human_count": human_count, "judges": {
            name: {"paired_count": score["paired_count"],
                   **{field: metric(score[field]) for field in
                      ("outcome_ok", "covered", "violated")},
                   "covered_by_id": {cid: metric(values)
                                     for cid, values in score["covered_by_id"].items()}}
            for name, score in scores.items()
        }}
