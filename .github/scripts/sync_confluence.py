import os
import sys
import hashlib
from datetime import datetime
from atlassian import Confluence
import markdown

# --- Configuration from environment ---
CONFLUENCE_URL = os.environ.get('CONFLUENCE_URL')
CONFLUENCE_USERNAME = os.environ.get('CONFLUENCE_USERNAME')
CONFLUENCE_API_TOKEN = os.environ.get('CONFLUENCE_API_TOKEN')
CONFLUENCE_SPACE_KEY = os.environ.get('CONFLUENCE_SPACE_KEY')
CONFLUENCE_PARENT_PAGE_ID = os.environ.get('CONFLUENCE_PARENT_PAGE_ID')

# NEW: Archive parent page id (set this in your secrets / environment)
CONFLUENCE_ARCHIVE_PARENT_PAGE_ID = os.environ.get('CONFLUENCE_ARCHIVE_PARENT_PAGE_ID')

DOCS_FOLDER = "docs"
ARCHIVE_FOLDER_TITLE = "Archive"  # Title used if we must create an archive page under the archive parent

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

def markdown_to_storage(md_content: str) -> str:
    """Converts Markdown content to Confluence storage format (HTML)."""
    html = markdown.markdown(md_content)
    return f'<div class="markdown-body">{html}</div>'

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
    If a page with folder_title already exists in the space and is under the same parent,
    return it. If a same-title page exists elsewhere, create a new one under parent_id
    (to avoid accidentally moving unrelated content).
    Returns the ID of the existing or newly created folder page.
    """
    # 1) Try to find an existing page with title under the desired parent using CQL (preferred)
    try:
        cql = f'title = "{folder_title}" AND ancestor = {parent_id} AND type = page'
        res = confluence.cql(cql, limit=1, expand='content.id,content.title')
        if res and res.get('results'):
            return res['results'][0]['content']['id']
    except Exception:
        # If CQL not available or fails, fall back
        pass

    # 2) If there's a page with that title anywhere, check if it's already under the parent
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
        # If we found an existing page but not under the parent, create a new one instead of moving
        print(f"Found page titled '{folder_title}' but not under parent {parent_id}. Creating a new folder page under desired parent to avoid moving unrelated content.")

    # 3) Create a new folder page under the requested parent
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

    # 4) Final fallback: attempt to re-query with CQL/get_page_by_title
    try:
        cql = f'title = "{folder_title}" AND ancestor = {parent_id} AND type = page'
        res = confluence.cql(cql, limit=1, expand='content.id')
        if res and res.get('results'):
            return res['results'][0]['content']['id']
    except Exception:
        pass

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
    2) Otherwise, ensure an 'Archive' page exists under CONFLUENCE_PARENT_PAGE_ID and return it.
    Returns the page id to use as the archive parent.
    """
    # 1) If explicit archive parent id provided, verify it exists
    if CONFLUENCE_ARCHIVE_PARENT_PAGE_ID:
        try:
            page = confluence.get_page_by_id(CONFLUENCE_ARCHIVE_PARENT_PAGE_ID, expand='id')
            if page and page.get('id'):
                return page['id']
        except Exception:
            print(f"Warning: CONFLUENCE_ARCHIVE_PARENT_PAGE_ID provided but not found: {CONFLUENCE_ARCHIVE_PARENT_PAGE_ID}")

    # 2) Create / ensure a page titled ARCHIVE_FOLDER_TITLE under the configured main parent
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
                CONFLUENCE_SPACE_KEY, start=start, limit=limit, expand='ancestors,body.storage,version'
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

    # --- 5. Determine Actions: Create, Update/Move, Archive (instead of Delete) ---
    pages_to_create = []
    pages_to_update_or_move = []
    pages_to_archive = []

    # Identify pages to create or update/move
    for key, local_info in local_markdown_pages.items():
        expected_parent_id = key[0]
        title = key[1]

        existing_in_correct_place = all_existing_confluence_pages_by_key.get(key)
        existing_anywhere_by_title = find_page_in_space_by_title(title)

        if not existing_in_correct_place and not existing_anywhere_by_title:
            # Page does not exist in Confluence at all -> Create
            pages_to_create.append(local_info)
        else:
            remote_info = existing_in_correct_place or existing_anywhere_by_title
            needs_move = str(remote_info['parent_id']) != str(expected_parent_id)
            needs_update = local_info['hash'] != remote_info['hash']

            if needs_move or needs_update:
                pages_to_update_or_move.append({
                    "id": remote_info['id'],
                    "title": local_info['title'],
                    "storage": local_info['storage'],
                    "filepath": local_info['filepath'],
                    "target_parent_id": expected_parent_id,
                    "version": remote_info['version'],
                    "current_parent_id": remote_info['parent_id'],
                })
            else:
                print(f"Up to date: {local_info['filepath']} -> '{title}' under parent {expected_parent_id}")

    # Identify pages to archive (previously delete)
    for remote_key, remote_info in all_existing_confluence_pages_by_key.items():
        page_id = remote_info['id']
        title = remote_info['title']

        # Skip the configured parent page itself
        if str(page_id) == str(CONFLUENCE_PARENT_PAGE_ID):
            continue

        # Skip pages that exist in our local markdown set
        if remote_key in local_markdown_pages:
            continue

        # Check if this is a managed folder page
        is_managed_folder_page = page_id in folder_parent_ids.values()

        # Check if this page has any children
        try:
            children_of_this_page = confluence.get_child_pages(page_id)
        except Exception:
            children_of_this_page = []

        if is_managed_folder_page and children_of_this_page:
            # Folder page still has children -> skip archival to avoid orphaning content
            print(
                f"Skipping archival of folder page '{title}' (ID {page_id}) "
                f"as it still has child pages."
            )
            continue

        # Otherwise, archive this page
        pages_to_archive.append(remote_info)

    # --- 6. Execute Actions ---

    # Ensure archive parent exists and get its page id
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
        move_desc = f"moving from parent {p['current_parent_id']} to {p['target_parent_id']}" if str(p['current_parent_id']) != str(p['target_parent_id']) else "updating content"
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

    # Archive pages no longer in Git repo (instead of deleting)
    archived_count = 0
    for p in pages_to_archive:
        print(f"Archiving page '{p['title']}' (ID {p['id']}) as it no longer exists in the Git repo.")
        try:
            # Build archival note to prepend to page content
            original_parent = p.get('parent_id') or "Unknown"
            timestamp = datetime.utcnow().isoformat() + "Z"
            archival_note = (
                f"<div><strong>Archived by sync</strong><br/>"
                f"Original parent ID: {original_parent}<br/>"
                f"Archived at (UTC): {timestamp}</div><hr/>"
            )
            existing_storage = p.get('storage', '')

            new_storage = archival_note + existing_storage

            # Move/update page to be under the Archive folder (archive_parent_page_id)
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
