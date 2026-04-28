import os
import sys
import hashlib
import re
from datetime import datetime

from atlassian import Confluence
import markdown

# --- Configuration from environment ---
CONFLUENCE_URL = os.environ.get('CONFLUENCE_URL')
CONFLUENCE_USERNAME = os.environ.get('CONFLUENCE_USERNAME')
CONFLUENCE_API_TOKEN = os.environ.get('CONFLUENCE_API_TOKEN')
CONFLUENCE_SPACE_KEY = os.environ.get('CONFLUENCE_SPACE_KEY')
CONFLUENCE_PARENT_PAGE_ID = os.environ.get('CONFLUENCE_PARENT_PAGE_ID')

# NEW: Archive parent page id
CONFLUENCE_ARCHIVE_PARENT_PAGE_ID = os.environ.get('CONFLUENCE_ARCHIVE_PARENT_PAGE_ID')

DOCS_FOLDER = "docs"
ARCHIVE_FOLDER_TITLE = "Archive"

confluence = Confluence(
    url=CONFLUENCE_URL,
    username=CONFLUENCE_USERNAME,
    password=CONFLUENCE_API_TOKEN,
    cloud=True,
)

# -------------------------------------------------
# Utility helpers
# -------------------------------------------------

def md5(text: str) -> str:
    """Generates an MD5 hash of the content for change detection."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def to_title(name: str) -> str:
    """Converts a file/folder name into a Confluence-friendly title."""
    return name.replace("-", " ").replace("_", " ").strip().title()


# -------------------------------------------------
# Mermaid support  ✅ NEW
# -------------------------------------------------

MERMAID_BLOCK_RE = re.compile(
    r"```mermaid\s*(.*?)```",
    re.DOTALL | re.IGNORECASE
)

def convert_mermaid_blocks(md_content: str) -> str:
    """
    Converts ```mermaid fenced blocks into Confluence Mermaid macros.
    """
    def replacer(match):
        mermaid_code = match.group(1).strip()
        return (
            '<ac:structured-macro ac:name="mermaid">'
            '<ac:plain-text-body><![CDATA[\n'
            f'{mermaid_code}\n'
            ']]></ac:plain-text-body>'
            '</ac:structured-macro>'
        )

    return MERMAID_BLOCK_RE.sub(replacer, md_content)


def markdown_to_storage(md_content: str) -> str:
    """
    Converts Markdown content to Confluence storage format (HTML),
    including Mermaid diagram support.
    """
    # Convert Mermaid first
    md_with_mermaid = convert_mermaid_blocks(md_content)

    # Then normal Markdown → HTML
    html = markdown.markdown(
        md_with_mermaid,
        extensions=["extra", "tables", "fenced_code"]
    )

    return f"""
{html}
"""


# -------------------------------------------------
# Confluence helpers (unchanged)
# -------------------------------------------------

def find_page_in_space_by_title(title: str):
    try:
        page = confluence.get_page_by_title(
            space=CONFLUENCE_SPACE_KEY,
            title=title,
            expand='ancestors,body.storage,version'
        )
        return page
    except Exception:
        return None


def ensure_folder_page(folder_title: str, parent_id: str) -> str:
    try:
        cql = f'title = "{folder_title}" AND ancestor = {parent_id} AND type = page'
        res = confluence.cql(cql, limit=1, expand='content.id')
        if res and res.get('results'):
            return res['results'][0]['content']['id']
    except Exception:
        pass

    existing = None
    try:
        existing = confluence.get_page_by_title(
            space=CONFLUENCE_SPACE_KEY,
            title=folder_title,
            expand='ancestors'
        )
    except Exception:
        pass

    if existing:
        for anc in existing.get('ancestors', []):
            if str(anc.get('id')) == str(parent_id):
                return existing['id']

    created = confluence.create_page(
        space=CONFLUENCE_SPACE_KEY,
        parent_id=parent_id,
        title=folder_title,
        body="",
        representation="storage",
    )

    if created and created.get('id'):
        return created['id']

    raise RuntimeError(f"Unable to ensure folder page '{folder_title}'")


def ensure_archive_parent() -> str:
    if CONFLUENCE_ARCHIVE_PARENT_PAGE_ID:
        try:
            page = confluence.get_page_by_id(CONFLUENCE_ARCHIVE_PARENT_PAGE_ID)
            if page and page.get('id'):
                return page['id']
        except Exception:
            print("Warning: Provided archive parent page not found.")

    return ensure_folder_page(
        ARCHIVE_FOLDER_TITLE,
        CONFLUENCE_PARENT_PAGE_ID
    )


# -------------------------------------------------
# Main
# -------------------------------------------------

def main():
    if not all([
        CONFLUENCE_URL,
        CONFLUENCE_USERNAME,
        CONFLUENCE_API_TOKEN,
        CONFLUENCE_SPACE_KEY,
        CONFLUENCE_PARENT_PAGE_ID,
    ]):
        print("Error: Missing required Confluence environment variables.")
        sys.exit(1)

    print("Starting Git → Confluence sync")

    # ---------------------------------------------
    # Build folder hierarchy
    # ---------------------------------------------
    folder_parent_ids = {"": CONFLUENCE_PARENT_PAGE_ID}

    if os.path.isdir(DOCS_FOLDER):
        for root, dirs, _ in os.walk(DOCS_FOLDER):
            rel = os.path.relpath(root, DOCS_FOLDER)
            folder_path = "" if rel == "." else rel.replace("\\", "/")
            parent_id = folder_parent_ids[folder_path]

            for d in dirs:
                sub = os.path.join(folder_path, d).replace("\\", "/")
                if sub in folder_parent_ids:
                    continue
                folder_parent_ids[sub] = ensure_folder_page(
                    to_title(d),
                    parent_id
                )

    # ---------------------------------------------
    # Read Markdown files
    # ---------------------------------------------
    local_pages = {}

    for root, _, files in os.walk(DOCS_FOLDER):
        rel = os.path.relpath(root, DOCS_FOLDER)
        folder_path = "" if rel == "." else rel.replace("\\", "/")
        parent_id = folder_parent_ids[folder_path]

        for f in files:
            if not f.endswith(".md"):
                continue

            path = os.path.join(root, f)
            with open(path, "r", encoding="utf-8") as fh:
                md_content = fh.read()

            name = os.path.splitext(f)[0]

            if folder_path == "" and name.lower() == "index":
                title = "Documentation Home"
            elif name.lower() == "index":
                title = to_title(os.path.basename(folder_path))
            else:
                title = to_title(name)

            storage = markdown_to_storage(md_content)

            local_pages[(parent_id, title)] = {
                "title": title,
                "parent_id": parent_id,
                "storage": storage,
                "hash": md5(storage),
                "filepath": path,
            }

    # ---------------------------------------------
    # Fetch all existing Confluence pages
    # ---------------------------------------------
    existing_by_key = {}
    start = 0
    limit = 200

    while True:
        pages = confluence.get_all_pages_from_space(
            CONFLUENCE_SPACE_KEY,
            start=start,
            limit=limit,
            expand="ancestors,body.storage,version"
        )
        if not pages:
            break

        for p in pages:
            parent_id = p["ancestors"][-1]["id"] if p.get("ancestors") else None
            storage = p["body"]["storage"]["value"]

            existing_by_key[(parent_id, p["title"])] = {
                "id": p["id"],
                "title": p["title"],
                "parent_id": parent_id,
                "storage": storage,
                "hash": md5(storage),
                "version": p["version"]["number"],
            }

        if len(pages) < limit:
            break
        start += limit

    # ---------------------------------------------
    # Determine changes
    # ---------------------------------------------
    pages_to_create = []
    pages_to_update = []
    pages_to_archive = []

    for k, local in local_pages.items():
        if k not in existing_by_key:
            pages_to_create.append(local)
            continue

        remote = existing_by_key[k]
        if local["hash"] != remote["hash"]:
            pages_to_update.append({
                "id": remote["id"],
                "title": local["title"],
                "storage": local["storage"],
                "parent_id": local["parent_id"],
            })

    for k, remote in existing_by_key.items():
        if k not in local_pages and remote["id"] != CONFLUENCE_PARENT_PAGE_ID:
            pages_to_archive.append(remote)

    # ---------------------------------------------
    # Execute
    # ---------------------------------------------
    archive_parent = ensure_archive_parent()

    for p in pages_to_create:
        confluence.create_page(
            space=CONFLUENCE_SPACE_KEY,
            parent_id=p["parent_id"],
            title=p["title"],
            body=p["storage"],
            representation="storage",
        )

    for p in pages_to_update:
        confluence.update_page(
            page_id=p["id"],
            title=p["title"],
            body=p["storage"],
            parent_id=p["parent_id"],
        )

    for p in pages_to_archive:
        note = (
            f"<p><b>Archived by sync</b></p>"
            f"<p>Archived at UTC: {datetime.utcnow().isoformat()}Z</p>"
        )

        confluence.update_page(
            page_id=p["id"],
            title=p["title"],
            body=note + p["storage"],
            parent_id=archive_parent,
        )

    print("✅ Sync completed successfully")


if __name__ == "__main__":
    main()