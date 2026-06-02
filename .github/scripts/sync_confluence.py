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
CONFLUENCE_ARCHIVE_PARENT_PAGE_ID = os.environ.get('CONFLUENCE_ARCHIVE_PARENT_PAGE_ID')

DOCS_FOLDER = "docs"
ARCHIVE_FOLDER_TITLE = "Archive"

confluence = Confluence(
    url=CONFLUENCE_URL,
    username=CONFLUENCE_USERNAME,
    password=CONFLUENCE_API_TOKEN,
    cloud=True,
)


def md5(text: str) -> str:
    """Generates an MD5 hash of the content for change detection."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def to_title(name: str) -> str:
    """Converts a file/folder name into a Confluence-friendly title."""
    return name.replace("-", " ").replace("_", " ").strip().title()


def mermaid_code_to_confluence_macro(diagram_code: str) -> str:
    """
    Converts mermaid diagram code into a Confluence storage format macro.
    Requires the 'Mermaid Diagrams for Confluence' marketplace app.
    """
    return (
        '<ac:structured-macro ac:name="mermaid" ac:schema-version="1">'
        '<ac:plain-text-body>'
        f'<![CDATA[{diagram_code}]]>'
        '</ac:plain-text-body>'
        '</ac:structured-macro>'
    )


def markdown_to_storage(md_content: str) -> str:
    """
    Converts Markdown content to Confluence storage format (HTML/XML).

    Key fix: Instead of using placeholders (which get corrupted by the
    markdown parser), we SPLIT the content into alternating segments of
    [markdown, mermaid, markdown, mermaid, ...], convert only the markdown
    segments to HTML, and directly inject Confluence macros for mermaid
    segments. This avoids any corruption by the markdown parser.
    """
    # Pattern to find ```mermaid ... ``` blocks
    mermaid_pattern = re.compile(r'```mermaid\s*\n(.*?)```', re.DOTALL | re.IGNORECASE)

    # Split the content into parts: alternating markdown and mermaid segments
    # re.split with a capturing group gives us [md, mermaid, md, mermaid, ...]
    parts = mermaid_pattern.split(md_content)

    result_html_parts = []

    for i, part in enumerate(parts):
        if i % 2 == 0:
            # Even index -> pure markdown segment (no mermaid), convert to HTML
            if part.strip():
                html = markdown.markdown(
                    part,
                    extensions=['fenced_code', 'tables', 'toc', 'codehilite']
                )
                result_html_parts.append(html)
        else:
            # Odd index -> this is the captured mermaid diagram code
            diagram_code = part.strip()
            confluence_macro = mermaid_code_to_confluence_macro(diagram_code)
            result_html_parts.append(confluence_macro)

    combined_html = "\n".join(result_html_parts)
    return f'<div class="markdown-body">{combined_html}</div>'


def find_page_in_space_by_title(title: str):
    """
    Finds a page in the Confluence space by its title.
    Returns the page details if found, else None.
    """
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
    """
    Ensures a Confluence page exists with the given folder_title under parent_id.
    - If a page with folder_title already exists AND is a descendant of parent_id, return its id.
    - If a same-title page exists elsewhere, create a NEW page under parent_id.
    - If it doesn't exist, create it.
    """
    # 1) Try to find a page with that title under parent_id using CQL
    try:
        cql = f'title = "{folder_title}" AND ancestor = {parent_id} AND type = page'
        res = confluence.cql(cql, limit=1, expand='content.id,content.title')
        if res and res.get('results'):
            return res['results'][0]['content']['id']
    except Exception:
        pass

    # 2) Check if page exists anywhere in the space
    existing = None
    try:
        existing = confluence.get_page_by_title(
            space=CONFLUENCE_SPACE_KEY,
            title=folder_title,
            expand='ancestors,body.storage,version'
        )
    except Exception:
        existing = None

    if existing:
        try:
            ancestors = existing.get('ancestors') or []
            for anc in ancestors:
                if str(anc.get('id')) == str(parent_id):
                    return existing['id']
        except Exception:
            pass
        print(
            f"Found page titled '{folder_title}' in space but not under parent {parent_id}. "
            f"Creating a new folder page under the desired parent."
        )

    # 3) Create the folder page under the requested parent
    try:
        created = confluence.create_page(
            space=CONFLUENCE_SPACE_KEY,
            parent_id=parent_id,
            title=folder_title,
            body="",
            representation="storage",
        )
        if created and isinstance(created, dict) and created.get('id'):
            return created['id']
    except Exception as e:
        print(f"Warning: create_page failed for '{folder_title}': {e}")

    # 4) Fallback: re-query via CQL
    try:
        cql = f'title = "{folder_title}" AND ancestor = {parent_id} AND type = page'
        res = confluence.cql(cql, limit=1, expand='content.id')
        if res and res.get('results'):
            return res['results'][0]['content']['id']
    except Exception:
        pass

    # 5) Last resort
    try:
        fallback = confluence.get_page_by_title(space=CONFLUENCE_SPACE_KEY, title=folder_title)
        if fallback and fallback.get('id'):
            return fallback['id']
    except Exception:
        pass

    raise RuntimeError(f"Unable to ensure or locate folder page '{folder_title}' under parent {parent_id}")


def ensure_archive_parent() -> str:
    """
    Ensures we have an actual page id to archive into.
    Priority:
    1) Use CONFLUENCE_ARCHIVE_PARENT_PAGE_ID if provided and valid.
    2) Otherwise, ensure an 'Archive' page exists under CONFLUENCE_PARENT_PAGE_ID.
    """
    if CONFLUENCE_ARCHIVE_PARENT_PAGE_ID:
        try:
            page = confluence.get_page_by_id(CONFLUENCE_ARCHIVE_PARENT_PAGE_ID, expand='id')
            if page and page.get('id'):
                return page['id']
        except Exception:
            print(f"Warning: CONFLUENCE_ARCHIVE_PARENT_PAGE_ID not found: {CONFLUENCE_ARCHIVE_PARENT_PAGE_ID}")

    return ensure_folder_page(ARCHIVE_FOLDER_TITLE, CONFLUENCE_PARENT_PAGE_ID)


def main():
    # --- 1. Initial Checks ---
    if not all([
        CONFLUENCE_URL,
        CONFLUENCE_USERNAME,
        CONFLUENCE_API_TOKEN,
        CONFLUENCE_SPACE_KEY,
        CONFLUENCE_PARENT_PAGE_ID,
    ]):
        print("Error: Missing required Confluence environment variables.")
        sys.exit(1)

    print(
        f"Starting sync: Markdown files from '{DOCS_FOLDER}' to Confluence space '{CONFLUENCE_SPACE_KEY}' "
        f"under parent page ID '{CONFLUENCE_PARENT_PAGE_ID}'."
    )

    # --- 2. Build Confluence Folder Hierarchy based on Git Repo ---
    folder_parent_ids = {"": CONFLUENCE_PARENT_PAGE_ID}

    if os.path.isdir(DOCS_FOLDER):
        for root, dirs, files in os.walk(DOCS_FOLDER):
            rel = os.path.relpath(root, DOCS_FOLDER)
            folder_path = "" if rel == "." else rel.replace("\\", "/")
            current_confluence_parent_id = folder_parent_ids[folder_path]

            for d in dirs:
                sub_folder_relative_path = os.path.join(folder_path, d).replace("\\", "/")
                if sub_folder_relative_path in folder_parent_ids:
                    continue
                folder_title = to_title(d)
                folder_page_id = ensure_folder_page(folder_title, current_confluence_parent_id)
                folder_parent_ids[sub_folder_relative_path] = folder_page_id
    else:
        print(f"Warning: '{DOCS_FOLDER}' directory not found. No Markdown files to process.")

    # --- 3. Discover Local Markdown Files & Prepare Their Content ---
    local_markdown_pages = {}

    if os.path.isdir(DOCS_FOLDER):
        for root, dirs, files in os.walk(DOCS_FOLDER):
            rel = os.path.relpath(root, DOCS_FOLDER)
            folder_path = "" if rel == "." else rel.replace("\\", "/")
            confluence_parent_id_for_current_folder = folder_parent_ids[folder_path]

            for filename in files:
                if not filename.endswith(".md"):
                    continue

                filepath = os.path.join(root, filename)
                with open(filepath, "r", encoding="utf-8") as f:
                    md_content = f.read()

                name_no_ext = os.path.splitext(filename)[0]

                if folder_path == "" and name_no_ext.lower() == "index":
                    title = "Documentation Home"
                elif name_no_ext.lower() == "index":
                    title = to_title(os.path.basename(folder_path))
                else:
                    title = to_title(name_no_ext)

                # Convert markdown to Confluence storage format
                # Mermaid blocks are split out BEFORE markdown parsing
                # and injected as Confluence macros AFTER — no corruption possible
                storage = markdown_to_storage(md_content)
                content_hash = md5(storage)

                key = (confluence_parent_id_for_current_folder, title)
                local_markdown_pages[key] = {
                    "title": title,
                    "storage": storage,
                    "hash": content_hash,
                    "parent_id": confluence_parent_id_for_current_folder,
                    "filepath": filepath,
                }

    # --- 4. Fetch ALL existing pages in the Confluence space ---
    all_existing_confluence_pages_by_key = {}
    all_existing_confluence_pages_by_id = {}

    start = 0
    limit = 200
    while True:
        try:
            pages_chunk = confluence.get_all_pages_from_space(
                CONFLUENCE_SPACE_KEY,
                start=start,
                limit=limit,
                expand='ancestors,body.storage,version'
            )
            if not pages_chunk:
                break

            for page in pages_chunk:
                page_id = page['id']
                title = page['title']
                parent_id = page['ancestors'][-1]['id'] if page.get('ancestors') else None
                storage = page.get('body', {}).get('storage', {}).get('value', '')
                version = page.get('version', {}).get('number', 1)

                page_info = {
                    "id": page_id,
                    "title": title,
                    "parent_id": parent_id,
                    "hash": md5(storage),
                    "version": version,
                    "storage": storage,
                }
                all_existing_confluence_pages_by_id[page_id] = page_info
                all_existing_confluence_pages_by_key[(parent_id, title)] = page_info

            if len(pages_chunk) < limit:
                break
            start += limit
        except Exception as e:
            print(f"Error fetching all pages from space (start={start}): {e}")
            sys.exit(1)

    # --- 5. Determine Actions: Create, Update/Move, Archive ---
    pages_to_create = []
    pages_to_update_or_move = []
    pages_to_archive = []

    # Identify pages to create or update/move
    for key, local_info in local_markdown_pages.items():
        expected_parent_id = key[0]
        title = key[1]

        existing_in_correct_place = all_existing_confluence_pages_by_key.get(key)
        existing_anywhere_by_title = find_page_in_space_by_title(title)

        if existing_in_correct_place:
            remote_info = existing_in_correct_place
        elif existing_anywhere_by_title:
            page = existing_anywhere_by_title
            try:
                remote_parent_id = page['ancestors'][-1]['id'] if page.get('ancestors') else None
            except Exception:
                remote_parent_id = None

            remote_storage = page.get('body', {}).get('storage', {}).get('value', '')
            remote_version = page.get('version', {}).get('number', 1)
            remote_info = {
                "id": page.get('id'),
                "title": page.get('title'),
                "parent_id": remote_parent_id,
                "storage": remote_storage,
                "version": remote_version,
                "hash": md5(remote_storage),
            }
        else:
            remote_info = None

        if not remote_info:
            pages_to_create.append(local_info)
            continue

        needs_move = str(remote_info.get('parent_id') or '') != str(expected_parent_id or '')
        needs_update = local_info['hash'] != remote_info.get('hash')

        if needs_move or needs_update:
            pages_to_update_or_move.append({
                "id": remote_info['id'],
                "title": local_info['title'],
                "storage": local_info['storage'],
                "filepath": local_info['filepath'],
                "target_parent_id": expected_parent_id,
                "version": remote_info.get('version'),
                "current_parent_id": remote_info.get('parent_id'),
            })
        else:
            print(f"Up to date: {local_info['filepath']} -> '{title}' under parent {expected_parent_id}")

    # Identify pages to archive
    for remote_key, remote_info in all_existing_confluence_pages_by_key.items():
        page_id = remote_info['id']
        title = remote_info['title']

        if str(page_id) == str(CONFLUENCE_PARENT_PAGE_ID):
            continue

        if remote_key in local_markdown_pages:
            continue

        is_managed_folder_page = page_id in folder_parent_ids.values()

        try:
            children_of_this_page = confluence.get_child_pages(page_id)
        except Exception:
            children_of_this_page = []

        if is_managed_folder_page and children_of_this_page:
            print(
                f"Skipping archival of folder page '{title}' (ID {page_id}) "
                f"as it still has child pages."
            )
            continue

        pages_to_archive.append(remote_info)

    # --- 6. Execute Actions ---

    # Ensure archive parent
    try:
        archive_parent_page_id = ensure_archive_parent()
    except Exception as e:
        print(f"Error ensuring archive parent: {e}")
        sys.exit(1)

    # Create new pages
    for p in pages_to_create:
        print(f"Creating page '{p['title']}' under parent {p['parent_id']} from {p['filepath']}.")
        try:
            confluence.create_page(
                space=CONFLUENCE_SPACE_KEY,
                parent_id=p["parent_id"],
                title=p["title"],
                body=p["storage"],
                representation="storage",
            )
            print(f"Successfully created '{p['title']}'.")
        except Exception as e:
            print(f"Error creating page '{p['title']}': {e}")

    # Update or move existing pages
    for p in pages_to_update_or_move:
        move_desc = (
            f"moving from parent {p['current_parent_id']} to {p['target_parent_id']}"
            if str(p['current_parent_id']) != str(p['target_parent_id'])
            else "updating content"
        )
        print(f"Processing page '{p['title']}' (ID {p['id']}): {move_desc} from {p['filepath']}.")
        try:
            confluence.update_page(
                page_id=p["id"],
                title=p["title"],
                body=p["storage"],
                parent_id=p["target_parent_id"],
            )
            print(f"Successfully updated/moved '{p['title']}'.")
        except Exception as e:
            print(f"Error updating/moving page '{p['title']}' (ID {p['id']}): {e}")

    # Archive pages no longer in Git repo
    archived_count = 0
    for p in pages_to_archive:
        print(f"Archiving page '{p['title']}' (ID {p['id']}) as it no longer exists in the Git repo.")
        try:
            original_parent = p.get('parent_id') or "Unknown"
            timestamp = datetime.utcnow().isoformat() + "Z"
            archival_note = (
                f'<div style="background:#fff3cd;border:1px solid #ffc107;padding:10px;'
                f'border-radius:4px;margin-bottom:10px;">'
                f'<strong>&#9888; Archived by sync</strong><br/>'
                f'Original parent ID: {original_parent}<br/>'
                f'Archived at (UTC): {timestamp}'
                f'</div><hr/>'
            )
            existing_storage = p.get('storage', '')
            new_storage = archival_note + existing_storage

            confluence.update_page(
                page_id=p["id"],
                title=p["title"],
                body=new_storage,
                parent_id=archive_parent_page_id,
            )
            archived_count += 1
            print(f"Successfully archived '{p['title']}' under archive parent (ID {archive_parent_page_id}).")
        except Exception as e:
            print(f"Error archiving page '{p['title']}' (ID {p['id']}): {e}")

    # --- 7. Summary ---
    print("\n========== Sync Summary ==========")
    print(f"Pages created  : {len(pages_to_create)}")
    print(f"Pages updated  : {len(pages_to_update_or_move)}")
    print(f"Pages archived : {archived_count}")
    print("===================================")
    print("Sync complete.")


if __name__ == "__main__":
    main()

