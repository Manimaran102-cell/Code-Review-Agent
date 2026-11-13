"""Write the FastAPI OpenAPI spec to openapi.json (run from the project root)."""

import json
from pathlib import Path

from app.api import app

out = Path("openapi.json")
out.write_text(json.dumps(app.openapi(), indent=2) + "\n", encoding="utf-8")
print(f"wrote {out} ({len(app.openapi()['paths'])} paths)")
