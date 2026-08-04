#!/usr/bin/env python3
"""Shared conversion/filtering utilities for the ChatGPT conversation exporter.

Ports the tree-walking and rendering logic from pionxzh/chatgpt-exporter's
src/api.ts (extractConversationResult) and src/exporter/markdown.ts to plain
Python. Used by chatgpt_export_cdp.py, which drives a real, logged-in Chrome
tab over the DevTools Protocol -- same reasoning as claude_export_cdp.py: a
bare HTTP client isn't a real browser and gets rejected.

ChatGPT's conversation shape differs from claude.ai's flat message list: it's
a tree keyed by node id ("mapping"), walked backwards from "current_node" via
each node's "parent" link, skipping system/context-injection nodes -- mirrors
extractConversationResult() in the reference project exactly.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from common import safe_filename

# Roles/content-types that the reference project excludes from exported output.
SKIP_ROLES = {"system"}
SKIP_CONTENT_TYPES = {"model_editable_context", "user_editable_context"}


def parse_api_timestamp(value):
    # create_time/update_time are typed `number | string` in the reference
    # project: usually a Unix timestamp (seconds), but some accounts/endpoints
    # return an ISO 8601 string (e.g. "2026-08-04T11:59:32.630117Z") instead.
    if value is None:
        return None
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


def extract_conversation_nodes(mapping, start_node_id):
    """Walk mapping backwards from start_node_id via parent links, oldest-first."""
    result = []
    current_id = start_node_id
    while current_id:
        node = mapping.get(current_id)
        if not node:
            break
        if node.get("parent") is None:
            break  # root

        message = node.get("message")
        if message and message.get("author", {}).get("role") not in SKIP_ROLES and (
            message.get("content", {}).get("content_type") not in SKIP_CONTENT_TYPES
        ):
            result.insert(0, node)

        current_id = node.get("parent")
    return result


def get_current_branch(conversation):
    mapping = conversation.get("mapping") or {}
    start_node_id = conversation.get("current_node")
    if not start_node_id:
        for node_id, node in mapping.items():
            if not node.get("children"):
                start_node_id = node_id
                break
    if not start_node_id:
        return []
    return extract_conversation_nodes(mapping, start_node_id)


def _message_text(node):
    message = node.get("message") or {}
    content = message.get("content") or {}
    parts = content.get("parts")
    if parts:
        return "\n".join(p for p in parts if isinstance(p, str))
    return content.get("text", "")


def convert_to_markdown(data, include_metadata):
    md = f"# {data.get('title') or 'ChatGPT Conversation'}\n\n"
    if include_metadata:
        created = parse_api_timestamp(data.get("create_time"))
        updated = parse_api_timestamp(data.get("update_time"))
        md += f"**Created:** {created.isoformat() if created else 'unknown'}\n"
        md += f"**Updated:** {updated.isoformat() if updated else 'unknown'}\n"
        md += "\n---\n\n"
    for node in get_current_branch(data):
        role = node["message"].get("author", {}).get("role", "unknown")
        sender = "**You**" if role == "user" else ("**ChatGPT**" if role == "assistant" else f"**{role}**")
        text = _message_text(node)
        if not text:
            continue
        md += f"{sender}:\n\n{text}\n\n---\n\n"
    return md


def convert_to_text(data, include_metadata):
    text = ""
    if include_metadata:
        text += f"{data.get('title') or 'ChatGPT Conversation'}\n\n---\n\n"
    user_seen = assistant_seen = False
    for node in get_current_branch(data):
        role = node["message"].get("author", {}).get("role", "unknown")
        body = _message_text(node)
        if not body:
            continue
        if role == "user":
            label = "H" if user_seen else "Human"
            user_seen = True
        elif role == "assistant":
            label = "A" if assistant_seen else "Assistant"
            assistant_seen = True
        else:
            label = role
        text += f"{label}: {body}\n\n"
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
    file_path = out_dir / f"chatgpt-{safe_filename(data.get('title') or data.get('id'))}.{ext}"
    file_path.write_text(content, encoding="utf-8")
    return file_path


def filter_conversations(conversations, after, before, date_field):
    """Date-range filter only: the /conversations list endpoint doesn't include
    model info (that's only known per-conversation after a full fetch, via
    extractModel(mapping) in the reference project), so unlike claude_export's
    equivalent, there's no --model filter here."""
    field = "create_time" if date_field == "created" else "update_time"
    result = []
    for conv in conversations:
        ts = conv.get(field)
        ts_dt = parse_api_timestamp(ts) if ts is not None else None
        if ts_dt:
            if after and ts_dt < after:
                continue
            if before and ts_dt > before:
                continue
        result.append(conv)
    return result
