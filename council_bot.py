#!/usr/bin/env python3
"""Telegram bridge for the LLM Council prompt (see ~/.claude/commands/council.md).

Long-polls a Telegram bot for messages from one authorized chat. Each message
becomes the council's question; the full 5-advisor + chairman deliberation
runs against the context of every exported AI conversation under exports*/ in
this repo, and only the Chairman's final call is sent back to Telegram.

Two backends, picked by COUNCIL_BACKEND in .env.local:
  - "api"     (default) calls the Anthropic API directly. Needs
              ANTHROPIC_API_KEY -- separate, pay-per-token billing.
  - "browser" drives a real, already-logged-in Chrome tab over the DevTools
              Protocol -- the same trick claude_export_cdp.py uses -- and
              rides your claude.ai subscription instead of an API key.
              Quick-POC only: this hits claude.ai's internal, undocumented
              chat endpoints (no system prompt, no thinking/effort control,
              more fragile than the API and more likely to run afoul of
              claude.ai's terms on automated use if left running long-term).
              Needs Chrome running with --remote-debugging-port=9222 and a
              claude.ai tab already logged in, plus CLAUDE_ORG_ID.

Setup:
    1. Fill in TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.local (see
       .env.local.example). For the "api" backend also fill in
       ANTHROPIC_API_KEY; for "browser" fill in CLAUDE_ORG_ID instead and
       launch Chrome per claude_export_cdp.py's docstring.
    2. pip install anthropic
    3. python council_bot.py
"""
import glob
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import anthropic

import cdp
from claude_export_cdp import require_org_id
from common import REPO_ROOT, load_env_local

MODEL = "claude-opus-5"
MAX_TOKENS = 24000
POLL_TIMEOUT = 30  # seconds, Telegram long-poll
TELEGRAM_MESSAGE_LIMIT = 4000  # Telegram's hard cap is 4096

# claude.ai's internal model slug for the browser backend (distinct from the
# public API model IDs above) -- per cyber-wojtek/Claude-API's constants.py.
CLAUDE_WEB_MODEL = os.environ.get("CLAUDE_WEB_MODEL", "claude-opus-5")

# The default toolset claude.ai's web client attaches to every completion
# request, per cyber-wojtek/Claude-API's _DEFAULT_TOOLS.
CLAUDE_WEB_TOOLS = [
    {"name": "web_search", "type": "web_search_v0"},
    {"name": "artifacts", "type": "artifacts_v0"},
    {"name": "repl", "type": "repl_v0"},
    {"name": "ask_user_input_v0", "type": "widget"},
    {"name": "weather_fetch", "type": "widget"},
    {"name": "recipe_display_v0", "type": "widget"},
    {"name": "places_map_display_v0", "type": "widget"},
    {"name": "message_compose_v1", "type": "widget"},
    {"name": "places_search", "type": "widget"},
    {"name": "fetch_sports_data", "type": "widget"},
]

COUNCIL_INSTRUCTIONS = """\
You are going to run an "LLM Council" deliberation on a question, entirely on your own, by \
simulating five independent advisors and a chairman. Follow the three stages exactly, doing \
all of the work internally in your reasoning.

You also have access to the person's exported AI conversation history (Claude and ChatGPT \
chats) as context -- either inline below or as an attached file named \
exported_conversations.txt. Use it whenever it's relevant to the question — for example, if \
asked to surface interesting threads to explore further, mine this history for genuinely \
underexplored or intriguing questions rather than inventing generic ones.

THE FIVE ADVISORS (each is a distinct persona — stay fully in character, do not let them agree \
by default):
- Advisor 1 — The Contrarian: only looks at what will fail. Surfaces risks, failure modes, and \
reasons this goes wrong.
- Advisor 2 — The First-Principles Thinker: rips apart every assumption baked into the question \
and rebuilds from the ground up.
- Advisor 3 — The Expansionist: finds the upside, the bigger opportunity, and the option not \
being seen.
- Advisor 4 — The Outsider: knows nothing about the relevant industry; reasons from common \
sense and naive questions, ignoring jargon and "how it's always done."
- Advisor 5 — The Executor: only cares about what to actually DO next. Concrete, sequenced, \
practical.

=== STAGE 1: INDEPENDENT ANSWERS ===
Each of the five advisors answers the question independently and in their own voice. They must \
NOT reference each other.

=== STAGE 2: ANONYMIZED PEER REVIEW ===
Relabel the five Stage 1 answers as "Response A, B, C, D, E" in a random order. Then, acting as \
each advisor in turn, have them review all five anonymized responses — including, unknowingly, \
their own — evaluate each briefly, and produce a ranking. Compute an aggregate ranking across \
all five reviewers.

=== STAGE 3: CHAIRMAN'S FINAL CALL ===
Act as the Chairman. Using all five answers and the peer rankings (pay special attention to the \
responses that ranked highest, and to any disagreement between advisors), synthesize ONE final \
answer to the question.

Do all of Stage 1, Stage 2, and the Chairman's synthesis internally — none of it should appear \
in your visible response. Your entire visible output must be only the Chairman's final call, in \
this exact form and nothing else (no headers, no stage recaps, no meta-commentary, no \
"Stage 1/2/3" labels):

THE CALL: <the single clearest, most direct answer to the question, as a concise paragraph or \
list — sized to fit a phone screen>\
"""


def load_context():
    # POC scoping: point at a single export file instead of globbing all of
    # them (useful while testing generation time against a small context).
    single_file = os.environ.get("COUNCIL_CONTEXT_FILE")
    if single_file:
        path = Path(single_file)
        if not path.is_absolute():
            path = REPO_ROOT / path
        text = path.read_text(encoding="utf-8", errors="replace")
        return f"=== {path.relative_to(REPO_ROOT)} ===\n{text}", 1

    paths = sorted(
        p for pattern in ("exports*/**/*.md", "exports*/**/*.txt")
        for p in glob.glob(str(REPO_ROOT / pattern), recursive=True)
    )
    parts = []
    for p in paths:
        text = Path(p).read_text(encoding="utf-8", errors="replace")
        parts.append(f"=== {Path(p).relative_to(REPO_ROOT)} ===\n{text}")
    return "\n\n".join(parts), len(paths)


def tg_call(token, method, params=None, timeout=POLL_TIMEOUT + 10):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(params or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if not body.get("ok"):
        raise RuntimeError(f"Telegram API error on {method}: {body}")
    return body["result"]


def tg_send_message(token, chat_id, text):
    for start in range(0, len(text), TELEGRAM_MESSAGE_LIMIT):
        chunk = text[start:start + TELEGRAM_MESSAGE_LIMIT]
        tg_call(token, "sendMessage", {"chat_id": chat_id, "text": chunk})


def extract_final_call(text):
    idx = text.find("THE CALL")
    remainder = text[idx:] if idx != -1 else text
    # Models sometimes wrap the label in markdown bold (**THE CALL:**); the
    # find() above already drops the opening **, so just drop the trailing one.
    remainder = re.sub(r"^(THE CALL:?)\*+", r"\1", remainder)
    return remainder.strip()


def ask_council(client, question, context_text):
    system = [
        {"type": "text", "text": COUNCIL_INSTRUCTIONS},
        {
            "type": "text",
            "text": f"<exported_conversations>\n{context_text}\n</exported_conversations>",
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        },
    ]
    with client.messages.stream(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        output_config={"effort": "high"},
        messages=[{"role": "user", "content": f"MY QUESTION: {question}"}],
    ) as stream:
        response = stream.get_final_message()
    text = "".join(block.text for block in response.content if block.type == "text")
    return extract_final_call(text)


def js_ensure_conversation(org_id, conv_id, name):
    """Create a scratch claude.ai conversation. 400/409 (already exists) is fine."""
    name_json = json.dumps(name)
    return f"""
(async () => {{
  const res = await fetch('https://claude.ai/api/organizations/{org_id}/chat_conversations', {{
    method: 'POST',
    credentials: 'include',
    headers: {{ 'Content-Type': 'application/json', Accept: 'application/json' }},
    body: JSON.stringify({{
      uuid: '{conv_id}',
      name: {name_json},
      include_conversation_preferences: true,
      is_temporary: false
    }})
  }});
  if (!res.ok && res.status !== 400 && res.status !== 409) {{
    throw new Error('create conversation HTTP ' + res.status + ': ' + await res.text());
  }}
  return true;
}})()
"""


def js_upload_context_file(org_id, conv_id, context_text):
    """Attach `context_text` to the conversation as a text-file upload (rather
    than pasting ~100K+ chars inline -- matches how claude.ai's own web client
    handles large pastes). Returns the file_uuid, or null if the upload failed
    (endpoint shape reverse-engineered and may not match exactly)."""
    context_json = json.dumps(context_text)
    return f"""
(async () => {{
  const CONTEXT_TEXT = {context_json};
  const blob = new Blob([CONTEXT_TEXT], {{ type: 'text/plain' }});
  const form = new FormData();
  form.append('file', blob, 'exported_conversations.txt');
  const res = await fetch(`https://claude.ai/api/organizations/{org_id}/conversations/{conv_id}/wiggle/upload-file`, {{
    method: 'POST',
    credentials: 'include',
    body: form
  }});
  if (!res.ok) return null;
  const uploaded = await res.json();
  return uploaded.file_uuid || uploaded.id || null;
}})()
"""


def js_send_completion(org_id, conv_id, prompt, model, tools, file_uuid):
    """Send `prompt` (with `file_uuid` attached, if any) and read the streamed
    reply back as plain text. Payload fields and SSE framing are
    reverse-engineered from cyber-wojtek/Claude-API's
    claude_webapi/{client,session}.py -- undocumented and may drift; see the
    backend note in the module docstring."""
    prompt_json = json.dumps(prompt)
    model_json = json.dumps(model)
    tools_json = json.dumps(tools)
    file_uuid_json = json.dumps(file_uuid)
    return f"""
(async () => {{
  const fileUuid = {file_uuid_json};
  const payload = {{
    attachments: [],
    files: fileUuid ? [fileUuid] : [],
    locale: 'en-US',
    model: {model_json},
    parent_message_uuid: '00000000-0000-4000-8000-000000000000',
    prompt: {prompt_json},
    rendering_mode: 'messages',
    sync_sources: [],
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    tools: {tools_json},
    turn_message_uuids: {{
      human_message_uuid: crypto.randomUUID(),
      assistant_message_uuid: crypto.randomUUID()
    }}
  }};
  const res = await fetch(`https://claude.ai/api/organizations/{org_id}/chat_conversations/{conv_id}/completion`, {{
    method: 'POST',
    credentials: 'include',
    headers: {{ 'Content-Type': 'application/json', Accept: 'text/event-stream' }},
    body: JSON.stringify(payload)
  }});
  if (!res.ok) throw new Error('completion HTTP ' + res.status + ': ' + await res.text());

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  let text = '';
  let messageLimit = null;
  while (true) {{
    const {{ done, value }} = await reader.read();
    if (done) break;
    buf += decoder.decode(value, {{ stream: true }});
    const lines = buf.split('\\n');
    buf = lines.pop();
    for (const line of lines) {{
      if (!line.startsWith('data:')) continue;
      const payloadLine = line.slice(5).trim();
      if (!payloadLine || payloadLine === '[DONE]') continue;
      let evt;
      try {{ evt = JSON.parse(payloadLine); }} catch (e) {{ continue; }}
      if (evt.type === 'content_block_delta' && evt.delta && evt.delta.type === 'text_delta') {{
        text += evt.delta.text;
      }} else if (typeof evt.completion === 'string') {{
        text = evt.completion;
      }} else if (evt.type === 'message_limit') {{
        // Usage-window metadata, not part of the answer -- kept out of `text`
        // so it never leaks into what gets sent to Telegram.
        messageLimit = evt;
      }}
    }}
  }}
  return JSON.stringify({{ text, messageLimit }});
}})()
"""


def js_delete_conversation(org_id, conversation_uuid):
    return f"""
(async () => {{
  await fetch('https://claude.ai/api/organizations/{org_id}/chat_conversations/{conversation_uuid}', {{
    method: 'DELETE',
    credentials: 'include'
  }});
  return true;
}})()
"""


def default_log(message, err=False):
    print(message, file=sys.stderr if err else sys.stdout)


def ask_council_browser(question, context_text, port=9222, log=default_log):
    org_id = require_org_id()
    tab = cdp.find_claude_tab(port)
    if not tab:
        raise RuntimeError(
            f"No open claude.ai tab found on the Chrome instance at port {port}. "
            "Launch Chrome with --remote-debugging-port={port} and log into claude.ai in that window."
        )
    conn = cdp.CDPConnection(tab["webSocketDebuggerUrl"])
    conv_id = str(uuid.uuid4())
    conv_name = f"Council: {' '.join(question.split())}"[:100]
    try:
        t0 = time.monotonic()
        conn.evaluate(js_ensure_conversation(org_id, conv_id, conv_name), timeout=30)
        t1 = time.monotonic()
        log(f"[timing] create conversation: {t1 - t0:.1f}s")

        file_uuid = conn.evaluate(js_upload_context_file(org_id, conv_id, context_text), timeout=60)
        t2 = time.monotonic()
        log(f"[timing] upload context file ({len(context_text)} chars): {t2 - t1:.1f}s (uploaded={bool(file_uuid)})")
        if not file_uuid:
            log("Warning: context file upload failed; question sent without exported-conversation context", err=True)

        prompt = f"{COUNCIL_INSTRUCTIONS}\n\nMY QUESTION: {question}"
        raw = conn.evaluate(
            js_send_completion(org_id, conv_id, prompt, CLAUDE_WEB_MODEL, CLAUDE_WEB_TOOLS, file_uuid),
            timeout=300,
        )
        t3 = time.monotonic()
        if raw is None:
            # No JS exception was raised, but the fetch/stream never resolved to a
            # string -- almost always the Chrome tab's execution context got
            # torn down mid-request (a navigation or reload in that same tab
            # while we had an in-flight request), not a bug in the completion
            # itself. Retrying against an untouched tab usually just works.
            raise RuntimeError(
                "claude.ai returned no text after "
                f"{t3 - t2:.1f}s -- the Chrome tab was likely navigated/reloaded "
                "mid-request. Leave the claude.ai tab alone while the bot is answering."
            )
        result = json.loads(raw)
        text = result["text"]
        if result.get("messageLimit"):
            # Usage-window metadata, logged for visibility only -- never sent
            # to Telegram (see js_send_completion for why it's kept separate).
            log(f"[usage] {result['messageLimit']}")
        log(f"[timing] generate answer: {t3 - t2:.1f}s ({len(text)} chars)")

        try:
            conn.evaluate(js_delete_conversation(org_id, conv_id))
        except Exception:  # noqa: BLE001 -- best-effort cleanup only
            pass
        return extract_final_call(text)
    finally:
        conn.close()


def main():
    load_env_local()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.local")
    chat_id = int(chat_id)

    backend = os.environ.get("COUNCIL_BACKEND", "api").lower()
    if backend == "api":
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            sys.exit("Set ANTHROPIC_API_KEY in .env.local (or set COUNCIL_BACKEND=browser)")
        client = anthropic.Anthropic(api_key=api_key)
        ask = lambda q, ctx: ask_council(client, q, ctx)
    elif backend == "browser":
        port = int(os.environ.get("CLAUDE_CDP_PORT", "9222"))
        ask = lambda q, ctx: ask_council_browser(q, ctx, port=port)
    else:
        sys.exit(f"Unknown COUNCIL_BACKEND: {backend!r} (expected 'api' or 'browser')")

    # Skip any backlog: start from whatever comes after the latest existing
    # update instead of replaying old messages on startup.
    latest = tg_call(token, "getUpdates", {"offset": -1})
    offset = (latest[-1]["update_id"] + 1) if latest else 0

    print("council_bot listening for Telegram messages...")
    while True:
        try:
            updates = tg_call(token, "getUpdates", {"offset": offset, "timeout": POLL_TIMEOUT})
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"Poll error: {e}", file=sys.stderr)
            time.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            message = update.get("message")
            if not message or "text" not in message:
                continue
            if message["chat"]["id"] != chat_id:
                continue  # ignore anyone but the configured chat

            question = message["text"]
            print(f"Question: {question}")
            try:
                context_text, n_files = load_context()
                print(f"Context: {n_files} exported conversation(s)")
                answer = ask(question, context_text)
            except Exception as e:  # noqa: BLE001
                print(f"Council failed: {e}", file=sys.stderr)
                tg_send_message(token, chat_id, f"Council failed: {e}")
                continue
            tg_send_message(token, chat_id, answer)
            print("Sent reply to Telegram.")


if __name__ == "__main__":
    main()
