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

# --- Confluence Connection ---
try:
    confluence = Confluence(
        url=CONFLUENCE_URL,
        username=CONFLUENCE_USERNAME,
        password=CONFLUENCE_API_TOKEN,
        cloud=True,
    )
except Exception as e:
    print(f"Error connecting to Confluence: {e}")
    sys.exit(1)

# --- Core Functions ---

def md5(text: str) -> str:
    """Generates an MD5 hash of the content for change detection."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()

def to_title(name: str) -> str:
    """Converts a file/folder name into a Confluence-friendly title."""
    return name.replace("-", " ").replace("_", " ").strip().title()

def markdown_to_storage(md_content: str) -> str:
    """
    Converts Markdown to Confluence storage format, with robust Mermaid support
    using an HTML comment as a placeholder to avoid being processed by the markdown converter.
    """
    mermaid_blocks = {}
    # Use an HTML comment as a placeholder, which markdown parsers ignore.
    placeholder_template = "<!--MERMAID_PLACEHOLDER_{}-->"
    
    def find_and_replace_mermaid(match):
        block_id = len(mermaid_blocks)
        mermaid_code = match.group(1).strip()
        
        # Create the Confluence macro for this block
        macro = (f'<ac:structured-macro ac:name="mermaid">'
                 f'<ac:parameter ac:name="width">100%</ac:parameter>'
                 f'<ac:plain-text-body><![CDATA[{mermaid_code}]]></ac:plain-text-body>'
                 f'</ac:structured-macro>')
        mermaid_blocks[block_id] = macro
        
        # Return the HTML comment placeholder
        return placeholder_template.format(block_id)

    # Regex to find mermaid code blocks
    mermaid_pattern = re.compile(r"```mermaid\n(.*?)\n```", re.DOTALL)
    
    # 1. Replace all mermaid blocks with HTML comment placeholders
    md_with_placeholders = mermaid_pattern.sub(find_and_replace_mermaid, md_content)
    
    # 2. Convert the remaining Markdown content to HTML. The placeholders will be untouched.
    html_body = markdown.markdown(md_with_placeholders, extensions=['fenced_code', 'tables'])
    
    # 3. Replace the placeholders in the generated HTML with the actual Confluence macros.
    final_html = html_body
    for block_id, macro in mermaid_blocks.items():
        final_html = final_html.replace(placeholder_template.format(block_id), macro)
        
    return f'<div class="markdown-body">{final_html}</div>'

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
    """
    Ensures a Confluence page for a folder exists under the correct parent and returns its ID.
    This function is critical for building the correct hierarchy.
    """
    # 1. First, try to find an existing page with this title under the correct parent. This is the ideal case.
    try:
        cql = f'title = "{folder_title}" AND ancestor = {parent_id} AND type = page'
        res = confluence.cql(cql, limit=1, expand='content.id')
        if res and res.get('results'):
            return res['results'][0]['content']['id']
    except Exception as e:
        print(f"  (Info) CQL query for existing folder page '{folder_title}' failed, will try other methods. Error: {e}")

    # 2. If not found via CQL, check if a page with this title exists anywhere else in the space.
    existing_page = find_page_in_space_by_title(folder_title)
    if existing_page:
        try:
            # If it exists, we must check if its parent matches our target parent_id.
            ancestors = existing_page.get('ancestors') or []
            if ancestors and str(ancestors[-1].get('id')) == str(parent_id):
                # The page exists and is already under the correct parent.
                return existing_page['id']
            else:
                # The page exists but is in the wrong location. To avoid data loss or moving unrelated pages,
                # we will create a new page under the correct parent instead of moving the existing one.
                print(f"  (Warning) Page titled '{folder_title}' exists but not under parent {parent_id}. Creating a new page to avoid moving unrelated content.")
        except Exception:
            pass # Continue to creation if ancestor check fails

    # 3. If no suitable page exists, create a new one.
    try:
        print(f"  Creating folder page: '{folder_title}' under parent ID {parent_id}")
        created_page = confluence.create_page(
            space=CONFLUENCE_SPACE_KEY,
            parent_id=parent_id,
            title=folder_title,
            body="",  # Folder pages have no body content
            representation="storage",
        )
        if created_page and isinstance(created_page, dict) and created_page.get('id'):
            return created_page['id']
    except Exception as e:
        print(f"  (Error) create_page API call failed for '{folder_title}': {e}")
    
    # 4. As a final fallback, re-query using CQL to find the page we may have just created.
    # This can help in cases where the API call is slow to return the ID.
    try:
        cql = f'title = "{folder_title}" AND ancestor = {parent_id} AND type = page'
        res = confluence.cql(cql, limit=1, expand='content.id')
        if res and res.get('results'):
            return res['results'][0]['content']['id']
    except Exception:
        pass

    # If we reach this point, we have failed to create or find the page.
    raise RuntimeError(f"FATAL: Unable to ensure or locate folder page '{folder_title}' under parent {parent_id}.")


def ensure_archive_parent() -> str:
    """
    Ensures the 'Archive' parent page exists and returns its ID.
    Uses CONFLUENCE_ARCHIVE_PARENT_PAGE_ID if set, otherwise creates an 'Archive' page.
    """
    # 1. Prefer the explicit environment variable if it's set and valid.
    if CONFLUENCE_ARCHIVE_PARENT_PAGE_ID:
        try:
            print(f"  Verifying archive parent ID: {CONFLUENCE_ARCHIVE_PARENT_PAGE_ID}")
            page = confluence.get_page_by_id(CONFLUENCE_ARCHIVE_PARENT_PAGE_ID, expand='id')
            if page and page.get('id'):
                return page['id']
        except Exception:
            print(f"  (Warning) CONFLUENCE_ARCHIVE_PARENT_PAGE_ID ('{CONFLUENCE_ARCHIVE_PARENT_PAGE_ID}') was not found. Will create a default archive page instead.")

    # 2. If the variable is not set or invalid, create/find a default 'Archive' page under the main parent.
    print(f"  Ensuring default '{ARCHIVE_FOLDER_TITLE}' page exists under main parent {CONFLUENCE_PARENT_PAGE_ID}.")
    return ensure_folder_page(ARCHIVE_FOLDER_TITLE, CONFLUENCE_PARENT_PAGE_ID)
# --- Main Execution ---

def main():
    """
    Main function to orchestrate the synchronization process.
    """
    # 1. --- Initial Checks & Setup ---
    print("--- 1. Verifying Configuration ---")
    if not all([CONFLUENCE_URL, CONFLUENCE_USERNAME, CONFLUENCE_API_TOKEN, CONFLUENCE_SPACE_KEY, CONFLUENCE_PARENT_PAGE_ID]):
        print("FATAL: Missing one or more required environment variables (CONFLUENCE_URL, USERNAME, API_TOKEN, SPACE_KEY, PARENT_PAGE_ID).")
        sys.exit(1)
    print(f"  - Syncing Markdown from local folder: '{DOCS_FOLDER}'")
    print(f"  - To Confluence Space: '{CONFLUENCE_SPACE_KEY}'")
    print(f"  - Under Parent Page ID: '{CONFLUENCE_PARENT_PAGE_ID}'")

    # 2. --- Build Folder Hierarchy in Confluence ---
    print("\n--- 2. Building Confluence Folder Hierarchy ---")
    folder_parent_ids = {"": CONFLUENCE_PARENT_PAGE_ID} # Maps local folder path to Confluence page ID
    if os.path.isdir(DOCS_FOLDER):
        # Walk through the docs folder and create a corresponding page for each sub-directory
        for root, dirs, _ in os.walk(DOCS_FOLDER):
            rel_path = os.path.relpath(root, DOCS_FOLDER)
            folder_path = "" if rel_path == "." else rel_path.replace("\\", "/")
            parent_id = folder_parent_ids[folder_path]
            
            for d in sorted(dirs): # Sort for consistent processing order
                sub_folder_path = os.path.join(folder_path, d).replace("\\", "/")
                if sub_folder_path not in folder_parent_ids:
                    folder_title = to_title(d)
                    folder_page_id = ensure_folder_page(folder_title, parent_id)
                    folder_parent_ids[sub_folder_path] = folder_page_id
    else:
        print(f"  (Warning) Docs folder '{DOCS_FOLDER}' not found. No files will be processed.")

    # 3. --- Discover and Prepare Local Markdown Files ---
    print("\n--- 3. Discovering Local Markdown Files ---")
    local_markdown_pages = {} # Maps a key (parent_id, title) to page details
    if os.path.isdir(DOCS_FOLDER):
        for root, _, files in os.walk(DOCS_FOLDER):
            rel_path = os.path.relpath(root, DOCS_FOLDER)
            folder_path = "" if rel_path == "." else rel_path.replace("\\", "/")
            parent_id = folder_parent_ids[folder_path]
            
            for filename in sorted(files):
                if not filename.endswith(".md"):
                    continue

                filepath = os.path.join(root, filename)
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        md_content = f.read()
                except Exception as e:
                    print(f"  (Error) Could not read file '{filepath}': {e}")
                    continue

                name_no_ext = os.path.splitext(filename)[0]
                # Special rule: title an 'index.md' file after its parent folder
                if name_no_ext.lower() == "index" and folder_path:
                    title = to_title(os.path.basename(folder_path))
                else:
                    title = to_title(name_no_ext)
                
                storage_content = markdown_to_storage(md_content)
                content_hash = md5(storage_content)
                key = (parent_id, title)

                local_markdown_pages[key] = {
                    "title": title,
                    "storage": storage_content,
                    "hash": content_hash,
                    "parent_id": parent_id,
                    "filepath": filepath,
                }
    print(f"  Found {len(local_markdown_pages)} local Markdown files to process.")

    # 4. --- Fetch All Existing Pages from Confluence ---
    print("\n--- 4. Fetching Existing Pages from Confluence ---")
    all_confluence_pages = {} # Maps a key (parent_id, title) to page details
    try:
        # get_all_pages_from_space can be slow on large spaces.
        all_pages_from_space = confluence.get_all_pages_from_space(CONFLUENCE_SPACE_KEY, expand='ancestors,body.storage,version')
        for page in all_pages_from_space:
            parent_id = page['ancestors'][-1]['id'] if page.get('ancestors') else None
            storage_val = page.get('body', {}).get('storage', {}).get('value', '')
            key = (parent_id, page['title'])
            all_confluence_pages[key] = {
                "id": page['id'],
                "hash": md5(storage_val),
                "version": page['version']['number']
            }
        print(f"  Found {len(all_confluence_pages)} existing pages in the space.")
    except Exception as e:
        print(f"FATAL: Error fetching all pages from Confluence: {e}")
        sys.exit(1)

    # 5. --- Determine Actions (Create, Update, Archive) ---
    print("\n--- 5. Determining Actions (Create, Update, Archive) ---")
    pages_to_create = []
    pages_to_update = []
    processed_keys = set()

    for key, local_page in local_markdown_pages.items():
        processed_keys.add(key)
        remote_page = all_confluence_pages.get(key)

        if not remote_page:
            pages_to_create.append(local_page)
        elif local_page['hash'] != remote_page['hash']:
            update_info = {**local_page, **remote_page} # Combine local and remote info
            pages_to_update.append(update_info)

    # Determine pages to archive: any page on Confluence not in our local set
    archive_parent_id = ensure_archive_parent()
    pages_to_archive = []
    for key, remote_page in all_confluence_pages.items():
        # A page should be archived if it's not in our local file list.
        # We also add several guards to prevent accidental archival of important pages.
        is_managed = key in local_markdown_pages
        # Guard against archiving the root parent page of the entire documentation structure
        is_main_sync_parent = str(remote_page.get('id')) == str(CONFLUENCE_PARENT_PAGE_ID)
        is_archive_page = str(remote_page.get('id')) == str(archive_parent_id)

        if not is_managed and not is_main_sync_parent and not is_archive_page:
            archive_info = {**remote_page, "title": key[1], "parent_id": key[0]}
            pages_to_archive.append(archive_info)

    # 6. --- Execute Actions ---
    print("\n--- 6. Executing Actions ---")
    
    # Create Pages
    if pages_to_create:
        print(f"\n  Creating {len(pages_to_create)} new page(s)...")
        for page in pages_to_create:
            print(f"    - CREATING: '{page['title']}' from '{page['filepath']}'")
            try:
                confluence.create_page(
                    space=CONFLUENCE_SPACE_KEY, parent_id=page['parent_id'],
                    title=page['title'], body=page['storage'], representation='storage'
                )
            except Exception as e:
                print(f"      (Error) Failed to create page '{page['title']}': {e}")
    
    # Update Pages
    if pages_to_update:
        print(f"\n  Updating {len(pages_to_update)} existing page(s)...")
        for page in pages_to_update:
            print(f"    - UPDATING: '{page['title']}' from '{page['filepath']}'")
            try:
                confluence.update_page(
                    page_id=page['id'], title=page['title'],
                    body=page['storage'], representation='storage'
                )
            except Exception as e:
                print(f"      (Error) Failed to update page '{page['title']}': {e}")

    # Archive Pages
    if pages_to_archive:
        print(f"\n  Archiving {len(pages_to_archive)} remote page(s) not found locally...")
        for page in pages_to_archive:
            print(f"    - ARCHIVING: '{page['title']}' (ID: {page['id']})")
            try:
                # To archive, we simply move the page under the archive parent
                confluence.update_page(
                    page_id=page['id'], title=page['title'],
                    parent_id=archive_parent_id,
                    version_comment="Archived by sync script because file no longer exists locally.",
                    minor_edit=True
                )
            except Exception as e:
                print(f"      (Error) Failed to archive page '{page['title']}': {e}")

    # 7. --- Final Summary ---
    print("\n--- 7. Sync Summary ---")
    print(f"  - Pages Created: {len(pages_to_create)}")
    print(f"  - Pages Updated: {len(pages_to_update)}")
    print(f"  - Pages Archived: {len(pages_to_archive)}")
    print("\nSync complete.")


if __name__ == "__main__":
    main()
