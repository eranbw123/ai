#!/usr/bin/env python3
"""Shared conversion/filtering utilities for the Claude conversation exporter.

Ports content.js's getCurrentBranch/convertToMarkdown/convertToText and
browse.js's model filter to plain Python. Used by claude_export_cdp.py,
which is the actual working export flow (drives a real, logged-in Chrome tab
over the DevTools Protocol -- a bare HTTP client here gets 403'd by
claude.ai's anti-bot protection, since it isn't a real browser).
"""
import json
from datetime import datetime
from pathlib import Path

from common import safe_filename


def parse_api_timestamp(value):
    # API timestamps look like "2025-06-01T12:34:56.789012Z"
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def get_current_branch(data):
    chat_messages = data.get("chat_messages")
    leaf_uuid = data.get("current_leaf_message_uuid")
    if not chat_messages or not leaf_uuid:
        return []

    message_map = {m["uuid"]: m for m in chat_messages}
    branch = []
    current_uuid = leaf_uuid
    while current_uuid and current_uuid in message_map:
        message = message_map[current_uuid]
        branch.insert(0, message)
        current_uuid = message.get("parent_message_uuid")
        if current_uuid not in message_map:
            break
    return branch


def _message_text(message):
    content = message.get("content")
    if content:
        return "".join(c.get("text", "") for c in content if c.get("text"))
    return message.get("text", "")


def convert_to_markdown(data, include_metadata):
    md = f"# {data.get('name') or 'Untitled Conversation'}\n\n"
    if include_metadata:
        md += f"**Created:** {data.get('created_at')}\n"
        md += f"**Updated:** {data.get('updated_at')}\n"
        md += f"**Model:** {data.get('model')}\n\n---\n\n"
    for message in get_current_branch(data):
        sender = "**You**" if message.get("sender") == "human" else "**Claude**"
        md += f"{sender}:\n\n{_message_text(message)}\n\n---\n\n"
    return md


def convert_to_text(data, include_metadata):
    text = ""
    if include_metadata:
        text += f"{data.get('name') or 'Untitled Conversation'}\nModel: {data.get('model')}\n\n---\n\n"
    human_seen = assistant_seen = False
    for message in get_current_branch(data):
        if message.get("sender") == "human":
            label = "H" if human_seen else "Human"
            human_seen = True
        else:
            label = "A" if assistant_seen else "Assistant"
            assistant_seen = True
        text += f"{label}: {_message_text(message)}\n\n"
    return text.strip()


def write_export(data, fmt, include_metadata, out_dir):
    if fmt == "markdown":
        content, ext = convert_to_markdown(data, include_metadata), "md"
    elif fmt == "text":
        content, ext = convert_to_text(data, include_metadata), "txt"
    else:
        content, ext = json.dumps(data, indent=2), "json"

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    file_path = out_dir / f"claude-{safe_filename(data.get('name') or data.get('uuid'))}.{ext}"
    file_path.write_text(content, encoding="utf-8")
    return file_path


def filter_conversations(conversations, after, before, date_field, model):
    field = "created_at" if date_field == "created" else "updated_at"
    result = []
    for conv in conversations:
        if model and conv.get("model") != model:
            continue
        ts = conv.get(field)
        if ts:
            ts_dt = parse_api_timestamp(ts)
            if after and ts_dt < after:
                continue
            if before and ts_dt > before:
                continue
        result.append(conv)
    return result
