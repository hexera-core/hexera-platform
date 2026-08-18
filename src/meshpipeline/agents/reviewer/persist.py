# Responsibility: Save the artifacts a review produced, so a verdict can be re-examined.
# Boundaries: storage of evidence already produced.
from __future__ import annotations

import json
import logging
from datetime import UTC
from datetime import datetime as _dt
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _make_image_replacer():
    _img_counter = [0]

    def _replace_images(content: Any) -> Any:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            result = []
            for item in content:
                if (isinstance(item, dict)
                        and item.get("type") == "image_url"
                        and isinstance(item.get("image_url"), dict)
                        and (item["image_url"].get("url", "")).startswith("data:image")):
                    _img_counter[0] += 1
                    result.append({"type": "image_url",
                                   "image_url": {"file": f"{_img_counter[0]:03d}.png"}})
                else:
                    result.append(item)
            return result
        return content

    return _replace_images


def save_review_artifacts(
    review_save_dir: Path,
    messages: list[dict],
    verdict_result: dict | None,
    manifest: dict,
    *,
    tool_call_count: int,
    retry_count: int,
    job_id: str,
) -> None:
    _replace_images = _make_image_replacer()
    try:
        conv_path = review_save_dir / "conversation.jsonl"
        conv_path.parent.mkdir(parents=True, exist_ok=True)
        with open(conv_path, "w", encoding="utf-8") as _cf:
            for msg in messages:
                record: dict = {"role": msg["role"]}
                record["content"] = _replace_images(msg.get("content"))
                if "tool_calls" in msg:
                    record["tool_calls"] = msg["tool_calls"]
                if "tool_call_id" in msg:
                    record["tool_call_id"] = msg["tool_call_id"]
                _cf.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        import traceback as _tb
        logger.warning("Reviewer: could not save conversation.jsonl - job_id=%s: %s\n%s",
                       job_id, exc, _tb.format_exc())

    try:
        _vj_patch_checks   = verdict_result.get("patch_checks", {})    if verdict_result else {}
        _vj_axis_findings  = verdict_result.get("axis_findings", {})   if verdict_result else {}
        _vj_rebuild        = bool(verdict_result.get("rebuild_required", False)) if verdict_result else False
        _vj_reasoning      = verdict_result.get("reasoning", "")       if verdict_result else ""
        _vj_verdict        = verdict_result.get("verdict", "FAIL")     if verdict_result else "FAIL"
        verdict_json = {
            "verdict":          _vj_verdict,
            "patch_checks":     _vj_patch_checks,
            "axis_findings":    _vj_axis_findings,
            "rebuild_required": _vj_rebuild,
            "reasoning":        _vj_reasoning,
            "tool_call_count": tool_call_count,
            "attempt_reviewed": retry_count,
            "saved_at":       _dt.now(UTC).isoformat(),
        }
        (review_save_dir / "verdict.json").write_text(
            json.dumps(verdict_json, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as exc:
        logger.warning("Reviewer: could not write verdict.json - job_id=%s: %s", job_id, exc)

    try:
        if manifest:
            _manifest_serial = {
                k: v for k, v in manifest.items()
                if k != "patch_cell_indices"
            }
            (review_save_dir / "mesh_manifest.json").write_text(
                json.dumps(_manifest_serial, indent=2, ensure_ascii=False), encoding="utf-8"
            )
    except Exception as exc:
        logger.warning("Reviewer: could not write mesh_manifest.json - job_id=%s: %s", job_id, exc)
