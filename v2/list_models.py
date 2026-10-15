"""Print the models your key can use. On OpenRouter: free models only (SHOW_ALL=true for all), with tool support."""
import json
import os
import sys
import urllib.request

base = (os.getenv("LLM_BASE_URL") or "https://openrouter.ai/api/v1").rstrip("/")
key = (os.getenv("LLM_API_KEY") or "").strip()
show_all = (os.getenv("SHOW_ALL") or "").lower() == "true"

req = urllib.request.Request(base + "/models")
if key:
    req.add_header("Authorization", f"Bearer {key}")
try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        models = json.load(resp)["data"]
except Exception as e:
    sys.exit(f"Could not list models from {base}: {e} "
             "(most providers other than OpenRouter need LLM_API_KEY set)")

if "openrouter.ai" in base and not show_all:
    models = [m for m in models if m["id"].endswith(":free")]
    print(f"{len(models)} free models (set SHOW_ALL=true to list all)\n")

print(f"{'MODEL ID':58} {'CONTEXT':>9}  TOOLS")
for m in sorted(models, key=lambda m: m["id"]):
    params = m.get("supported_parameters")
    tools = "?" if params is None else ("yes" if "tools" in params else "no")
    print(f"{m['id']:58} {str(m.get('context_length') or '?'):>9}  {tools}")
