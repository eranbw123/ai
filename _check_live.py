import sys
sys.path.insert(0, "C:/github")
import chatgpt_export_cdp as gc
import claude_export_cdp as cc

print("--- chatgpt newest 5 (raw, unfiltered) ---")
gconn = gc.connect(9222)
token = gconn.evaluate(gc.js_get_access_token())
page = gconn.evaluate(gc.js_fetch_conversations_page(token, 0, 5))
for item in page.get("items", []):
    print(item.get("id"), repr(item.get("title")), "create=", item.get("create_time"), "update=", item.get("update_time"))
gconn.close()

print("--- claude newest 5 (raw, unfiltered) ---")
org_id = cc.require_org_id()
cconn = cc.connect(9222)
page = cconn.evaluate(cc.js_fetch_conversations_page(org_id, 0, 5))
for item in page.get("data", []):
    print(item.get("uuid"), repr(item.get("name")), "create=", item.get("created_at"), "update=", item.get("updated_at"))
cconn.close()
