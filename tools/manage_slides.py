#!/usr/bin/env python3
from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from PIL import Image, ImageColor, ImageOps, UnidentifiedImageError

DISCORD_API = "https://discord.com/api/v10"
IMAGE_EXTENSIONS = re.compile(r"\.(png|jpe?g|webp|gif|bmp)$", re.IGNORECASE)


class PermanentOperationError(Exception):
    """再試行しても改善しない入力・権限エラー。"""


@dataclass
class Paths:
    root: Path
    queue: Path
    docs: Path
    state: Path
    result: Path
    config: Path


class DiscordClient:
    def __init__(self, token: str) -> None:
        if not token:
            raise RuntimeError("DISCORD_BOT_TOKEN が設定されていません。")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bot {token}",
                "User-Agent": "discord-vrc-slideshow-actions/1.0",
            }
        )

    def get_message(self, channel_id: str, message_id: str) -> dict[str, Any]:
        response = self.session.get(
            f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}", timeout=30
        )
        if response.status_code == 404:
            raise PermanentOperationError("元のDiscordメッセージが見つかりません。")
        response.raise_for_status()
        return response.json()

    def download(self, url: str, max_bytes: int) -> bytes:
        response = requests.get(url, timeout=60, stream=True, headers={"User-Agent": "discord-vrc-slideshow-actions/1.0"})
        response.raise_for_status()
        content_length = int(response.headers.get("Content-Length", "0") or "0")
        if content_length > max_bytes:
            raise PermanentOperationError("画像ファイルがサイズ上限を超えています。")

        buffer = io.BytesIO()
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if not chunk:
                continue
            buffer.write(chunk)
            if buffer.tell() > max_bytes:
                raise PermanentOperationError("画像ファイルがサイズ上限を超えています。")
        return buffer.getvalue()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def parse_color(value: str) -> tuple[int, int, int]:
    try:
        rgb = ImageColor.getrgb(value)
    except ValueError as exc:
        raise RuntimeError(f"background_color が不正です: {value}") from exc
    return rgb[:3]


def image_attachment(attachment: dict[str, Any]) -> bool:
    content_type = str(attachment.get("content_type") or "")
    filename = str(attachment.get("filename") or "")
    return content_type.startswith("image/") or bool(IMAGE_EXTENSIONS.search(filename))


def transform_image(source: bytes, destination: Path, config: dict[str, Any]) -> None:
    width = int(config["output_width"])
    height = int(config["output_height"])
    quality = int(config["jpeg_quality"])
    mode = str(config["resize_mode"])
    background = parse_color(str(config["background_color"]))

    try:
        with Image.open(io.BytesIO(source)) as opened:
            image = ImageOps.exif_transpose(opened)
            if getattr(image, "is_animated", False):
                image.seek(0)
            image.load()

            if image.mode in ("RGBA", "LA") or "transparency" in image.info:
                rgba = image.convert("RGBA")
                base = Image.new("RGBA", rgba.size, background + (255,))
                image = Image.alpha_composite(base, rgba).convert("RGB")
            else:
                image = image.convert("RGB")

            if mode == "cover":
                output = ImageOps.fit(
                    image,
                    (width, height),
                    method=Image.Resampling.LANCZOS,
                    centering=(0.5, 0.5),
                )
            elif mode == "contain":
                fitted = ImageOps.contain(image, (width, height), Image.Resampling.LANCZOS)
                output = Image.new("RGB", (width, height), background)
                x = (width - fitted.width) // 2
                y = (height - fitted.height) // 2
                output.paste(fitted, (x, y))
            else:
                raise RuntimeError("resize_mode は contain または cover を指定してください。")

            destination.parent.mkdir(parents=True, exist_ok=True)
            output.save(
                destination,
                format="JPEG",
                quality=quality,
                optimize=True,
                progressive=True,
                exif=b"",
            )
    except (UnidentifiedImageError, OSError) as exc:
        raise PermanentOperationError("添付ファイルを画像として読み込めませんでした。") from exc


def write_public_files(paths: Paths, state: dict[str, Any], config: dict[str, Any]) -> None:
    revision = int(state["revision"])
    slides = [int(item["id"]) for item in state["slides"]]
    updated_at = str(state["updated_at"])

    manifest = (
        f"revision={revision}\n"
        f"updated={updated_at}\n"
        f"slides={','.join(str(slide_id) for slide_id in slides)}\n"
    )
    manifest_path = paths.docs / "manifests" / f"{revision:06d}.txt"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(manifest, encoding="utf-8", newline="\n")

    latest = f"revision={revision}\nupdated={updated_at}\n"
    latest_dir = paths.docs / "latest"
    latest_dir.mkdir(parents=True, exist_ok=True)
    for slot in range(int(config["latest_cache_slots"])):
        (latest_dir / f"{slot:03d}.txt").write_text(latest, encoding="utf-8", newline="\n")

    save_json(paths.state, state)


def allocate_slide_id(state: dict[str, Any], config: dict[str, Any]) -> int:
    slide_id = int(state.get("next_id", 1))
    maximum = int(config["max_issued_slide_id"])
    if slide_id > maximum:
        raise PermanentOperationError(
            f"発行可能な画像番号の上限 {maximum} に達しています。ワールド側URL枠を増やしてください。"
        )
    state["next_id"] = slide_id + 1
    return slide_id


def process_add(
    item: dict[str, Any], state: dict[str, Any], paths: Paths, config: dict[str, Any], discord: DiscordClient
) -> dict[str, Any]:
    channel_id = str(item["channel_id"])
    message_id = str(item["message_id"])
    author_id = str(item["author_id"])
    message = discord.get_message(channel_id, message_id)

    if str(message.get("author", {}).get("id", "")) != author_id:
        raise PermanentOperationError("Discord投稿者情報が一致しません。")

    attachments = [attachment for attachment in message.get("attachments", []) if image_attachment(attachment)]
    if not attachments:
        raise PermanentOperationError("対象メッセージに画像添付がありません。")

    next_id = int(state.get("next_id", 1))
    maximum = int(config["max_issued_slide_id"])
    if next_id + len(attachments) - 1 > maximum:
        raise PermanentOperationError(
            f"発行可能な画像番号の上限 {maximum} に達しています。ワールド側URL枠を増やしてください。"
        )

    temp_dir = Path(tempfile.mkdtemp(prefix="slideshow-", dir=str(paths.root / "tools")))
    prepared: list[tuple[dict[str, Any], Path]] = []
    try:
        for index, attachment in enumerate(attachments):
            image_bytes = discord.download(str(attachment["url"]), int(config["max_source_bytes"]))
            temp_path = temp_dir / f"{index:03d}.jpg"
            transform_image(image_bytes, temp_path, config)
            prepared.append((attachment, temp_path))

        added: list[dict[str, Any]] = []
        for attachment, temp_path in prepared:
            slide_id = allocate_slide_id(state, config)
            filename = f"{slide_id:06d}.jpg"
            destination = paths.docs / "slides" / filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(temp_path), str(destination))
            record = {
                "id": slide_id,
                "filename": filename,
                "uploader_id": author_id,
                "source_channel_id": channel_id,
                "source_message_id": message_id,
                "original_filename": str(attachment.get("filename") or "image"),
                "created_at": utc_now(),
            }
            state["slides"].append(record)
            added.append(record)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    return {
        "status": "success",
        "type": "add",
        "channel_id": channel_id,
        "source_message_id": message_id,
        "owner_id": author_id,
        "slides": added,
    }


def authorized(record: dict[str, Any], item: dict[str, Any]) -> bool:
    return bool(item.get("requester_is_admin")) or str(record["uploader_id"]) == str(item["requester_id"])


def delete_records(state: dict[str, Any], records: list[dict[str, Any]], paths: Paths) -> None:
    delete_ids = {int(record["id"]) for record in records}
    for record in records:
        image_path = paths.docs / "slides" / str(record["filename"])
        if image_path.exists():
            image_path.unlink()
    state["slides"] = [record for record in state["slides"] if int(record["id"]) not in delete_ids]


def process_delete(item: dict[str, Any], state: dict[str, Any], paths: Paths) -> dict[str, Any]:
    slide_id = int(item["slide_id"])
    record = next((record for record in state["slides"] if int(record["id"]) == slide_id), None)
    if record is None:
        raise PermanentOperationError(f"スライド {slide_id:06d} は既に削除されています。")
    if not authorized(record, item):
        raise PermanentOperationError("この画像を削除する権限がありません。")

    delete_records(state, [record], paths)
    return {
        "status": "success",
        "type": "delete",
        "channel_id": str(item["channel_id"]),
        "management_message_id": str(item.get("management_message_id") or ""),
        "slides": [record],
    }


def process_delete_source(item: dict[str, Any], state: dict[str, Any], paths: Paths) -> dict[str, Any]:
    source_message_id = str(item["source_message_id"])
    candidates = [
        record for record in state["slides"] if str(record["source_message_id"]) == source_message_id
    ]
    if not candidates:
        raise PermanentOperationError("この投稿から登録された公開中の画像はありません。")

    permitted = [record for record in candidates if authorized(record, item)]
    if not permitted:
        raise PermanentOperationError("この投稿の画像を削除する権限がありません。")

    delete_records(state, permitted, paths)
    return {
        "status": "success",
        "type": "delete_source",
        "channel_id": str(item["channel_id"]),
        "source_message_id": source_message_id,
        "slides": permitted,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--queue", default="queue")
    parser.add_argument("--docs", default="docs")
    parser.add_argument("--state", default="data/state.json")
    parser.add_argument("--config", default="tools/config.json")
    parser.add_argument("--result", default="tools/result.json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    paths = Paths(
        root=root,
        queue=root / args.queue,
        docs=root / args.docs,
        state=root / args.state,
        config=root / args.config,
        result=root / args.result,
    )
    config = load_json(paths.config, {})
    state = load_json(
        paths.state,
        {"revision": 0, "next_id": 1, "updated_at": utc_now(), "slides": []},
    )
    state.setdefault("slides", [])
    state.setdefault("next_id", 1)
    state.setdefault("revision", 0)

    discord = DiscordClient(os.environ.get("DISCORD_BOT_TOKEN", ""))
    queue_files = sorted(paths.queue.glob("*.json"))
    operations: list[dict[str, Any]] = []
    content_changed = False
    repository_changed = False

    for queue_file in queue_files:
        item = load_json(queue_file, {})
        try:
            operation_type = item.get("type")
            if operation_type in ("add", "delete", "delete_source") and int(state["revision"]) >= int(config["max_revision"]):
                raise PermanentOperationError(
                    f"revision上限 {config['max_revision']} に達しています。Unity側URL枠を増やして再設定してください。"
                )
            if operation_type == "add":
                result = process_add(item, state, paths, config, discord)
            elif operation_type == "delete":
                result = process_delete(item, state, paths)
            elif operation_type == "delete_source":
                result = process_delete_source(item, state, paths)
            else:
                raise PermanentOperationError(f"未対応のキュー種別です: {operation_type}")

            operations.append({"queue_file": queue_file.name, **result})
            queue_file.unlink()
            repository_changed = True
            content_changed = True
        except PermanentOperationError as exc:
            operations.append(
                {
                    "queue_file": queue_file.name,
                    "status": "failure",
                    "type": item.get("type", "unknown"),
                    "channel_id": str(item.get("channel_id", "")),
                    "source_message_id": str(item.get("message_id", item.get("source_message_id", ""))),
                    "management_message_id": str(item.get("management_message_id", "")),
                    "error": str(exc),
                }
            )
            queue_file.unlink()
            repository_changed = True
        except (requests.RequestException, OSError) as exc:
            # 一時的な通信・ファイルエラーはキューを残し、次回再試行する。
            print(f"一時エラーのため再試行します: {queue_file.name}: {exc}", file=sys.stderr)
            raise

    if content_changed:
        state["revision"] = int(state["revision"]) + 1
        state["updated_at"] = utc_now()
        write_public_files(paths, state, config)

    result_document = {
        "repository_changed": repository_changed,
        "content_changed": content_changed,
        "revision": int(state["revision"]),
        "operations": operations,
        "generated_at": utc_now(),
    }
    save_json(paths.result, result_document)

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as handle:
            handle.write(f"repository_changed={'true' if repository_changed else 'false'}\n")
            handle.write(f"content_changed={'true' if content_changed else 'false'}\n")
            handle.write(f"revision={state['revision']}\n")

    print(json.dumps(result_document, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
