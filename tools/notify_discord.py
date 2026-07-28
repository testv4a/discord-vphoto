#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

API = "https://discord.com/api/v10"
PENDING = "⏳"
SUCCESS = "✅"
ERROR = "❌"


class DiscordNotifier:
    def __init__(self, token: str) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
        )

    def request(self, method: str, path: str, payload: Any | None = None) -> Any:
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                response = self.session.request(
                    method,
                    f"{API}{path}",
                    json=payload,
                    timeout=30,
                )
                if response.status_code == 429:
                    retry_after = float(response.json().get("retry_after", 1))
                    time.sleep(retry_after)
                    continue
                response.raise_for_status()
                return None if response.status_code == 204 else response.json()
            except requests.RequestException as exc:
                last_error = exc
                time.sleep(2**attempt)
        raise RuntimeError(f"Discord APIに接続できません: {last_error}")

    def reaction(self, channel_id: str, message_id: str, emoji: str, add: bool) -> None:
        method = "PUT" if add else "DELETE"
        self.request(
            method,
            f"/channels/{channel_id}/messages/{message_id}/reactions/{quote(emoji)}/@me",
        )

    def message(self, channel_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", f"/channels/{channel_id}/messages", payload)

    def edit_message(self, channel_id: str, message_id: str, payload: dict[str, Any]) -> None:
        self.request("PATCH", f"/channels/{channel_id}/messages/{message_id}", payload)


def load(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def add_notification(notifier: DiscordNotifier, operation: dict[str, Any], pages_base_url: str) -> None:
    channel_id = operation["channel_id"]
    source_id = operation["source_message_id"]
    owner_id = operation["owner_id"]

    notifier.reaction(channel_id, source_id, PENDING, False)
    notifier.reaction(channel_id, source_id, SUCCESS, True)

    for slide in operation["slides"]:
        slide_id = int(slide["id"])
        image_url = f"{pages_base_url.rstrip('/')}/slides/{slide['filename']}"
        notifier.message(
            channel_id,
            {
                "content": (
                    "✅ スライドへ登録しました\n"
                    f"管理番号: {slide_id:06d}\n"
                    f"登録者: <@{owner_id}>\n"
                    f"公開URL: {image_url}"
                ),
                "message_reference": {"message_id": source_id},
                "allowed_mentions": {"parse": []},
                "components": [
                    {
                        "type": 1,
                        "components": [
                            {
                                "type": 2,
                                "style": 4,
                                "label": "削除する",
                                "custom_id": f"slide_delete|{slide_id:06d}|{owner_id}",
                            }
                        ],
                    }
                ],
            },
        )


def delete_notification(notifier: DiscordNotifier, operation: dict[str, Any]) -> None:
    channel_id = operation["channel_id"]
    ids = [f"{int(slide['id']):06d}" for slide in operation["slides"]]
    management_id = operation.get("management_message_id")
    if management_id:
        notifier.edit_message(
            channel_id,
            management_id,
            {
                "content": f"🗑️ スライド {', '.join(ids)} は削除済みです。",
                "components": [],
            },
        )
    notifier.message(
        channel_id,
        {"content": f"✅ スライド {', '.join(ids)} を削除しました。ワールド内の更新ボタンで反映できます。"},
    )


def failure_notification(notifier: DiscordNotifier, operation: dict[str, Any]) -> None:
    channel_id = operation.get("channel_id")
    if not channel_id:
        return
    source_id = operation.get("source_message_id")
    if source_id:
        notifier.reaction(channel_id, source_id, PENDING, False)
        notifier.reaction(channel_id, source_id, ERROR, True)
        notifier.message(
            channel_id,
            {
                "content": f"❌ スライド処理に失敗しました: {operation.get('error', '不明なエラー')}",
                "message_reference": {"message_id": source_id},
                "allowed_mentions": {"replied_user": False},
            },
        )
    else:
        notifier.message(
            channel_id,
            {"content": f"❌ スライド削除に失敗しました: {operation.get('error', '不明なエラー')}"},
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", default="tools/result.json")
    parser.add_argument("--pages-base-url", required=True)
    parser.add_argument("--deploy-status", default="success")
    args = parser.parse_args()

    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN が設定されていません。")

    document = load(Path(args.result))
    notifier = DiscordNotifier(token)
    deploy_ok = args.deploy_status in ("success", "skipped")

    for operation in document.get("operations", []):
        if operation.get("status") != "success":
            failure_notification(notifier, operation)
            continue

        if operation["type"] == "add":
            if deploy_ok:
                add_notification(notifier, operation, args.pages_base_url)
            else:
                operation["error"] = "GitHub Pagesへの公開に失敗しました。"
                failure_notification(notifier, operation)
        elif operation["type"] in ("delete", "delete_source"):
            if deploy_ok:
                delete_notification(notifier, operation)
            else:
                operation["error"] = "GitHub Pagesへの公開に失敗しました。"
                failure_notification(notifier, operation)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
