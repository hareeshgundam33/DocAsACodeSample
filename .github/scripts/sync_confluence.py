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

def convert_mermaid_to_macro(md_content: str) -> str:
    """Finds Mermaid code blocks and wraps them in the Confluence macro."""
    # This regex finds mermaid code blocks
    mermaid_pattern = re.compile(r"```mermaid\n(.*?)\n```", re.DOTALL)
    
    def replace_with_macro(match):
        mermaid_code = match.group(1).strip()
        # The macro requires the code to be in a CDATA section
        return (f'<ac:structured-macro ac:name="mermaid">'
                f'<ac:parameter ac:name="width">100%</ac:parameter>'
                f'<ac:plain-text-body><![CDATA[{mermaid_code}]]></ac:plain-text-body>'
                f'</ac:structured-macro>')

    return mermaid_pattern.sub(replace_with_macro, md_content)

def markdown_to_storage(md_content: str) -> str:
    """Converts Markdown to Confluence storage format, with Mermaid support."""
    # First, handle Mermaid blocks
    md_with_macros = convert_mermaid_to_macro(md_content)
    
    # Then, convert the remaining markdown to HTML
    # Note: We pass the content with macros to markdown. It will ignore the macros
    # and convert the actual markdown syntax around it.
    html = markdown.markdown(md_with_macros, extensions=['fenced_code', 'tables'])
    
    return f'<div class="markdown-body">{html}</div>'

def find_page_in_space_by_title(title: str):
    """Finds a page in the Confluence space by its title."""
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
    """Ensures a Confluence page for a folder exists and returns its ID."""
    # Try to find an existing page with this title under the correct parent first
    try:
        cql = f'title = "{folder_title}" AND ancestor = {parent_id} AND type = page'
        res = confluence.cql(cql, limit=1, expand='content.id')
        if res and res.get('results'):
            return res['results'][0]['content']['id']
    except Exception:
        pass # Fallback to other methods

    # If a page with the same title exists elsewhere, create a new one to be safe
    existing = find_page_in_space_by_title(folder_title)
    if existing:
        try:
            # Check if it's already in the right place
            ancestors = existing.get('ancestors') or []
            for anc in ancestors:
                if str(anc.get('id')) == str(parent_id):
                    return existing['id']
        except Exception:
            pass
        print(f"Found page titled '{folder_title}' but not under parent {parent_id}. Creating new page.")

    # If no suitable page exists, create one
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
    
    # As a last resort, re-query to find the page we might have created
    try:
        cql = f'title = "{folder_title}" AND ancestor = {parent_id} AND type = page'
        res = confluence.cql(cql, limit=1, expand='content.id')
        if res and res.get('results'):
            return res['results'][0]['content']['id']
    except Exception:
        pass

    raise RuntimeError(f"Unable to ensure or locate folder page '{folder_title}' under parent {parent_id}")

def ensure_archive_parent() -> str:
    """Ensures the archive parent page exists and returns its ID."""
    if CONFLUENCE_ARCHIVE_PARENT_PAGE_ID:
        try:
            page = confluence.get_page_by_id(CONFLUENCE_ARCHIVE_PARENT_PAGE_ID, expand='id')
            if page and page.get('id'):
                return page['id']
        except Exception:
            print(f"Warning: CONFLUENCE_ARCHIVE_PARENT_PAGE_ID ('{CONFLUENCE_ARCHIVE_PARENT_PAGE_ID}') not found.")

    # If the env var is not set or invalid, create/find a page under the main parent
    return ensure_folder_page(ARCHIVE_FOLDER_TITLE, CONFLUENCE_PARENT_PAGE_ID)


def main():
    # --- 1. Initial Checks ---
    if not all([CONFLUENCE_URL, CONFLUENCE_USERNAME, CONFLUENCE_API_TOKEN, CONFLUENCE_SPACE_KEY, CONFLUENCE_PARENT_PAGE_ID]):
        print("Error: Missing required Confluence environment variables.")
        sys.exit(1)

    print(f"Starting sync from '{DOCS_FOLDER}' to space '{CONFLUENCE_SPACE_KEY}' under page ID '{CONFLUENCE_PARENT_PAGE_ID}'.")

    # --- 2. Build Folder Hierarchy ---
    folder_parent_ids = {"": CONFLUENCE_PARENT_PAGE_ID}
    if os.path.isdir(DOCS_FOLDER):
        # First pass: create all folder pages
        for root, dirs, _ in os.walk(DOCS_FOLDER):
            rel_path = os.path.relpath(root, DOCS_FOLDER)
            folder_path = "" if rel_path == "." else rel_path.replace("\\", "/")
            parent_id = folder_parent_ids[folder_path]
            
            for d in sorted(dirs): # Sort for consistent ordering
                sub_folder_path = os.path.join(folder_path, d).replace("\\", "/")
                if sub_folder_path not in folder_parent_ids:
                    folder_title = to_title(d)
                    folder_page_id = ensure_folder_page(folder_title, parent_id)
                    folder_parent_ids[sub_folder_path] = folder_page_id
    else:
        print(f"Warning: '{DOCS_FOLDER}' directory not found. No files to process.")

    # --- 3. Discover and Prepare Local Files ---
    local_markdown_pages = {}
    if os.path.isdir(DOCS_FOLDER):
        for root, _, files in os.walk(DOCS_FOLDER):
            rel_path = os.path.relpath(root, DOCS_FOLDER)
            folder_path = "" if rel_path == "." else rel_path.replace("\\", "/")
            parent_id = folder_parent_ids[folder_path]
            
            for filename in files:
                if not filename.endswith(".md"):
                    continue

                filepath = os.path.join(root, filename)
                with open(filepath, "r", encoding="utf-8") as f:
                    md_content = f.read()

                name_no_ext = os.path.splitext(filename)[0]
                # Special handling for index files to title them after their parent folder
                if name_no_ext.lower() == "index" and folder_path:
                    title = to_title(os.path.basename(folder_path))
                else:
                    title = to_title(name_no_ext)
                
                storage = markdown_to_storage(md_content)
                content_hash = md5(storage)

                key = (parent_id, title)
                local_markdown_pages[key] = {
                    "title": title,
                    "storage": storage,
                    "hash": content_hash,
                    "parent_id": parent_id,
                    "filepath": filepath,
                }

    # --- 4. Fetch All Pages from Confluence ---
    all_confluence_pages = {}
    try:
        all_pages = confluence.get_all_pages_from_space(CONFLUENCE_SPACE_KEY, expand='ancestors,body.storage,version')
        for page in all_pages:
            parent_id = page['ancestors'][-1]['id'] if page.get('ancestors') else None
            storage_val = page.get('body', {}).get('storage', {}).get('value', '')
            key = (parent_id, page['title'])
            all_confluence_pages[key] = {
                "id": page['id'],
                "hash": md5(storage_val),
                "version": page['version']['number']
            }
    except Exception as e:
        print(f"Error fetching all pages from Confluence: {e}")
        sys.exit(1)

    # --- 5. Determine Actions ---
    to_create = []
    to_update = []
    processed_keys = set()

    for key, local_page in local_markdown_pages.items():
        processed_keys.add(key)
        remote_page = all_confluence_pages.get(key)

        if not remote_page:
            to_create.append(local_page)
        elif local_page['hash'] != remote_page['hash']:
            # Combine local and remote info for the update action
            update_info = {**local_page, **remote_page}
            to_update.append(update_info)

    # Determine pages to archive
    archive_parent_id = ensure_archive_parent()
    to_archive = []
    for key, remote_page in all_confluence_pages.items():
        # Only archive pages that are not in the local set and are not the main parent
        if key not in processed_keys and str(key[0]) != str(CONFLUENCE_PARENT_PAGE_ID):
            archive_info = {**remote_page, "title": key[1], "parent_id": key[0]}
            to_archive.append(archive_info)


    # --- 6. Execute Actions ---
    for page in to_create:
        print(f"Creating: {page['filepath']} -> '{page['title']}'")
        try:
            confluence.create_page(
                space=CONFLUENCE_SPACE_KEY,
                parent_id=page['parent_id'],
                title=page['title'],
                body=page['storage'],
                representation='storage'
            )
        except Exception as e:
            print(f"  ERROR creating '{page['title']}': {e}")

    for page in to_update:
        print(f"Updating: {page['filepath']} -> '{page['title']}'")
        try:
            confluence.update_page(
                page_id=page['id'],
                title=page['title'],
                body=page['storage'],
                # parent_id is not needed here unless you're moving the page, which is handled by folder creation
            )
        except Exception as e:
            print(f"  ERROR updating '{page['title']}': {e}")

    for page in to_archive:
        print(f"Archiving: '{page['title']}' (ID: {page['id']})")
        try:
            # For this simplified script, we just move the page. 
            # A more advanced version could prepend an "Archived" banner to the body.
            confluence.update_page(
                page_id=page['id'],
                title=page['title'],
                body="", # Clearing the body, or you could fetch existing and prepend a note
                parent_id=archive_parent_id
            )
        except Exception as e:
            print(f"  ERROR archiving '{page['title']}': {e}")


    # --- 7. Summary ---
    print("\n========== Sync Summary ==========")
    print(f"Pages to create: {len(to_create)}")
    print(f"Pages to update:  {len(to_update)}")
    print(f"Pages to archive: {len(to_archive)}")
    print("==================================")
    print("Sync complete.")

if __name__ == "__main__":
    main()
