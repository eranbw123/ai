"""Claude without an API key: one prompt in, one reply out, driven through a
real, already-authenticated claude.ai tab over the DevTools Protocol.

This is the repo's only LLM transport. There is deliberately no `anthropic`
import here and no API key: every completion is a scratch conversation
created inside the owner's own logged-in browser session (create -> POST
/completion -> read the SSE stream -> delete), which is how this machine has
always talked to Claude. The endpoint shapes are undocumented internals
reverse-engineered from cyber-wojtek/Claude-API and carry the same drift
caveat council_bot.py has always carried.

The js_* builders and parse_completion_result() below were council_bot.py's
until interest_extractor.py needed the same round trip. They moved here
rather than being copied, and council_bot.py imports them back from this
module -- so there is still exactly one implementation of the claude.ai
payload shapes to keep working when they drift. council_bot.py additionally
imports `anthropic` for its API backend; this module must not, so that the
extractor can use the browser path without dragging an SDK dependency (and
an API key) behind it.

What the caller gives up versus an API client, and what this module does
about it:
  * No structured outputs. complete_json() asks for bare JSON, slices the
    first {...} out of whatever comes back, and retries once with a sterner
    instruction before failing.
  * No token metering. The SSE stream carries no usage counts, so cost
    tracking is not possible here (and not needed -- this rides the owner's
    existing claude.ai subscription).
  * One serialized tab. The connection is reused across calls, and the tab
    is shared with anything else driving that Chrome, so callers are
    expected to checkpoint their own progress between calls rather than
    assume a long run survives intact.

Needs: Chrome on --remote-debugging-port (default 9222), a claude.ai tab
logged in inside it, and CLAUDE_ORG_ID in .env.local.
"""
import json
import re
import time
import uuid

import cdp
from claude_export_cdp import require_org_id

DEFAULT_PORT = 9222
DEFAULT_MODEL = "claude-opus-5"

# Bound on the "is the tab reachable at all" check -- a port that accepts
# connections but never answers must not hang a preflight forever. Real
# completions use their own, much longer timeouts.
PREFLIGHT_TIMEOUT_SECONDS = 5

STRICT_JSON_SUFFIX = (
    "\n\nRespond with ONLY a JSON object -- no prose, no markdown fences, "
    "nothing before or after the object. It must match this shape:\n{shape}"
)

RETRY_SUFFIX = (
    "\n\nIMPORTANT: your previous answer was not parseable as JSON. Reply "
    "with the bare JSON object itself, every required key present, and "
    "nothing else."
)


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
    module docstring."""
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


def parse_completion_result(raw):
    """Parse whatever conn.evaluate() handed back for js_send_completion.

    js_send_completion's JS always returns `JSON.stringify({text,
    messageLimit})` -- a string -- so this is normally just json.loads(raw).
    But at least one Chrome/CDP combination has been observed handing the
    value back already deserialized into a dict instead of the JSON string
    (json.loads(a_dict) then blows up with a confusing TypeError). Accept
    both shapes rather than crashing the whole question on it.
    """
    if isinstance(raw, dict):
        return raw
    return json.loads(raw)


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)


def extract_json_object(text):
    """Pull the JSON object out of a reply that may be fenced or narrated.

    claude.ai has no structured-output mode, so a reply is free to open with
    "Here's the JSON:" or wrap the payload in a fence no matter how firmly
    the prompt says otherwise. Slicing first '{' to last '}' survives both,
    and survives a trailing "Let me know if..." as well.
    """
    stripped = _FENCE_RE.sub("", text or "")
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"no JSON object found in reply: {(text or '')[:200]!r}")
    return stripped[start : end + 1]


class BrowserClaudeError(RuntimeError):
    """The browser round trip ran but produced nothing usable."""


class BrowserClaude:
    """A reusable claude.ai session. One scratch conversation per call.

    `connect` is injectable purely so tests never open a socket -- it must
    return something with .evaluate(js, timeout=) and .close(), i.e. a
    cdp.CDPConnection.
    """

    def __init__(self, *, org_id=None, port=DEFAULT_PORT, model=DEFAULT_MODEL,
                 connect=None, log=None):
        self.org_id = org_id
        self.port = port
        self.model = model
        self._connect = connect or self._connect_chrome
        self._connection = None
        self._log = log or (lambda msg: None)
        self.calls = 0

    # --- reachability ---------------------------------------------------------

    def preflight(self):
        """Free, local check: is there a claude.ai tab to talk to at all?
        Returns (ok, reason). Never raises -- a dead port and a non-CDP
        listener answering with non-JSON (a ValueError) are both just "not
        reachable"."""
        try:
            self._require_org()
        except BrowserClaudeError as e:
            return False, str(e)
        try:
            tab = cdp.find_claude_tab(self.port)
        except (OSError, ValueError) as e:
            return False, f"no Chrome DevTools endpoint on port {self.port} ({e})"
        if not tab:
            return False, f"no open claude.ai tab in the Chrome on port {self.port}"
        return True, ""

    # --- the round trip -------------------------------------------------------

    def complete(self, prompt, *, timeout=300, context=None):
        """One prompt -> the reply text. `context`, if given, is uploaded as a
        text file attachment instead of being pasted inline (claude.ai's own
        client does the same for large pastes)."""
        org_id = self._require_org()
        conn = self._conn()
        conv_id = str(uuid.uuid4())
        started = time.monotonic()
        try:
            conn.evaluate(js_ensure_conversation(org_id, conv_id, "interest-extractor scratch"), timeout=30)
            file_uuid = None
            if context:
                file_uuid = conn.evaluate(js_upload_context_file(org_id, conv_id, context), timeout=120)
                if not file_uuid:
                    # Fall back to inline rather than silently dropping the
                    # context: a digest built from a prompt whose corpus went
                    # missing would look like a successful empty result.
                    prompt = f"{prompt}\n\n{context}"
            raw = conn.evaluate(
                js_send_completion(org_id, conv_id, prompt, self.model, [], file_uuid),
                timeout=timeout,
            )
        except RuntimeError as e:
            # A JS exception is claude.ai refusing (HTTP error, expired
            # session), not a broken socket -- the connection stays usable.
            raise BrowserClaudeError(f"claude.ai call failed: {e}") from e
        except (ConnectionError, OSError) as e:
            self._reset()
            raise BrowserClaudeError(f"claude.ai connection failed: {e}") from e
        finally:
            try:
                conn.evaluate(js_delete_conversation(org_id, conv_id), timeout=15)
            except Exception:  # noqa: BLE001 -- best-effort cleanup only
                pass

        self.calls += 1
        if raw is None:
            raise BrowserClaudeError(
                "claude.ai returned no text -- the Chrome tab was most likely "
                "navigated or reloaded mid-request; leave the claude.ai tab alone"
            )
        result = parse_completion_result(raw)
        text = (result or {}).get("text") or ""
        if result.get("messageLimit"):
            self._log(f"[usage] {result['messageLimit']}")
        if not text.strip():
            raise BrowserClaudeError("empty completion from claude.ai")
        self._log(f"[timing] completion: {time.monotonic() - started:.1f}s ({len(text)} chars)")
        return text

    def complete_json(self, prompt, shape, *, timeout=300, context=None):
        """complete() plus "and it must be JSON". One retry with a sterner
        instruction, because an unparseable first reply is common enough on a
        transport with no structured-output mode to be worth absorbing here
        rather than in every caller."""
        base = prompt + STRICT_JSON_SUFFIX.format(shape=shape)
        last_error = None
        for attempt in range(2):
            text = self.complete(base if attempt == 0 else base + RETRY_SUFFIX,
                                  timeout=timeout, context=context)
            try:
                parsed = json.loads(extract_json_object(text))
            except (ValueError, TypeError) as e:
                last_error = BrowserClaudeError(f"reply attempt {attempt + 1} was not JSON: {e}")
                continue
            if not isinstance(parsed, dict):
                last_error = BrowserClaudeError(
                    f"reply attempt {attempt + 1} was a {type(parsed).__name__}, not a JSON object")
                continue
            return parsed
        raise last_error

    # --- session --------------------------------------------------------------

    def _require_org(self):
        if self.org_id:
            return self.org_id
        try:
            self.org_id = require_org_id()
        except SystemExit as e:
            # require_org_id() exits the process on a missing/invalid id --
            # right for a CLI entry point, wrong for a library call inside a
            # long resumable backfill, which wants to report and stop cleanly.
            raise BrowserClaudeError(str(e)) from e
        return self.org_id

    def _conn(self):
        if self._connection is None:
            self._connection = self._connect()
        return self._connection

    def _connect_chrome(self):
        tab = cdp.find_claude_tab(self.port)
        if not tab:
            raise BrowserClaudeError(
                f"No open claude.ai tab found on the Chrome instance at port {self.port}. "
                "Launch Chrome with --remote-debugging-port and log into claude.ai in that window."
            )
        return cdp.CDPConnection(tab["webSocketDebuggerUrl"])

    def _reset(self):
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception:  # noqa: BLE001
                pass
        self._connection = None

    def close(self):
        self._reset()
