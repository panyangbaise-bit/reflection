import json
from pathlib import Path

from portfolio_backend.api import create_app

app = create_app()
Path("docs/openapi.json").write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n")
app.state.db.close()
print("Wrote docs/openapi.json")
