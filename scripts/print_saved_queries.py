"""Print the saved queries the tools depend on, ready to hand to a Nexthink administrator.

  python scripts/print_saved_queries.py            # readable list
  python scripts/print_saved_queries.py --json     # machine-readable
"""
import json
import sys

from nexthink_mcp.client import SAVED_QUERIES

if "--json" in sys.argv:
    print(json.dumps([{"id": q.default_id, "key": q.key, "mode": q.mode, "parameters": list(q.parameters), "optional": q.optional,
                       "purpose": q.purpose, "nql": q.nql} for q in SAVED_QUERIES.values()], indent=2))
else:
    for q in SAVED_QUERIES.values():
        print(f"ID:         {q.default_id}" + ("   (optional)" if q.optional else ""))
        print(f"Used for:   {q.purpose}")
        print(f"Called via: {'synchronous execute' if q.mode == 'execute' else 'export'}" + (f"; parameters: {', '.join(q.parameters)}" if q.parameters else ""))
        print("NQL:\n" + "\n".join("    " + line for line in q.nql.split("\n")) + "\n")
