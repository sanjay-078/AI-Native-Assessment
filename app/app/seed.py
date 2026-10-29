import json
import os
from pathlib import Path

from .db import migrate, tx


def seed():
    migrate()
    documents = json.loads((Path(os.environ["DATA_DIR"]) / "documents.json").read_text())
    with tx() as conn:
        for d in documents:
            conn.execute(
                """INSERT INTO documents(tenant_id,document_id,version,status,authority,title,body)
                   VALUES(%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (tenant_id,document_id,version)
                   DO UPDATE SET status=EXCLUDED.status,authority=EXCLUDED.authority,
                   title=EXCLUDED.title,body=EXCLUDED.body""",
                (d["tenant_id"], d["document_id"], d["version"], d["status"],
                 d["authority"], d["title"], d["text"]),
            )
    print(f"Imported {len(documents)} documents")


if __name__ == "__main__":
    seed()
