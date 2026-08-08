import sqlite3
c = sqlite3.connect("C:/github/conversations.db")
for r in c.execute("SELECT source, title, created_at, imported_at, raw_source FROM raw_conversations ORDER BY imported_at DESC LIMIT 8"):
    print(r)
