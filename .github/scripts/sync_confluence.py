import os
import sys
import hashlib
import re
import json
import requests  # For direct low-level API calls
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
    print(f"FATAL: Error connecting to Confluence. Check URL, username, and API token. Error: {e}")
    sys.exit(1)

# --- Helper functions ---

def md5(text: str) -> str:
    """Generates an MD5 hash of the content for change detection."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()

def to_title(name: str) -> str:
    """Converts a file/folder name into a Confluence-friendly title."""
    return name.replace("-", " ").replace("_", " ").strip().title()

def markdown_to_storage(md_content: str) -> str:
    """
    Converts Markdown to Confluence storage format using split parts.
    Inserts direct 'code' macros for Mermaid, bypassing sanitizer.
    """
    mermaid_pattern = re.compile(r"(```mermaid\n.*?\n```)", re.DOTALL)
    parts = mermaid_pattern.split(md_content)
    
    final_html_parts = []
    
    for part in parts:
        if part.startswith("```mermaid"):
            inner_content_match = re.search(r"```mermaid\n(.*?)\n```", part, re.DOTALL)
            if inner_content_match:
                mermaid_code = inner_content_match.group(1).strip().replace(u'\xa0', u' ')
                macro = (f'<ac:structured-macro ac:name="code">'
                         f'<ac:parameter ac:name="language">mermaid</ac:parameter>'
                         f'<ac:plain-text-body><![CDATA[{mermaid_code}]]></ac:plain-text-body>'
                         f'</ac:structured-macro>')
                final_html_parts.append(macro)
        elif part.strip():
            html_part = markdown.markdown(part, extensions=['fenced_code', 'tables'])
            final_html_parts.append(html_part)
            
    final_html = "".join(final_html_parts)
    return final_html

# Direct HTTP API helper to create/update Confluence pages, bypassing sanitizer
def confluence_api_request(method, url, data, headers=None):
    if headers is None:
        headers = {}
    headers.update({
        'Content-Type': 'application/json',
        'Authorization': f'Basic {confluence._session.auth[1]}', # Use base64 encoded auth from atlassian lib
    })
    response = requests.request(method, url, headers=headers, data=json.dumps(data))
    if not response.ok:
        raise Exception(f"Confluence API {method} failed: {response.status_code} {response.text}")
    return response.json()
# --- Page handling via direct REST API calls ---

def create_page_direct(space, parent_id, title, storage_body):
    """
    Creates a Confluence page bypassing atlassian-python-api sanitizer,
    preserving raw storage format including Mermaid macros.
    """
    url = f"{CONFLUENCE_URL}/wiki/rest/api/content/"
    data = {
        "type": "page",
        "title": title,
        "space": {"key": space},
        "ancestors": [{"id": parent_id}],
        "body": {
            "storage": {
                "value": storage_body,
                "representation": "storage"
            }
        }
    }
    return confluence_api_request("POST", url, data)

def update_page_direct(page_id, title, storage_body, version):
    """
    Updates a Confluence page bypassing sanitization. 'version' must be current + 1.
    """
    url = f"{CONFLUENCE_URL}/wiki/rest/api/content/{page_id}"
    data = {
        "version": {"number": version + 1},
        "title": title,
        "type": "page",
        "body": {
            "storage": {
                "value": storage_body,
                "representation": "storage",
            }
        }
    }
    return confluence_api_request("PUT", url, data)

def get_page_version(page_id):
    """
    Retrieves the current version number of a page.
    """
    url = f"{CONFLUENCE_URL}/wiki/rest/api/content/{page_id}?expand=version"
    response = requests.get(url, headers={
        'Authorization': f'Basic {confluence._session.auth[1]}',
        'Content-Type': 'application/json',
    })
    if not response.ok:
        raise Exception(f"Failed to get page version: {response.status_code} {response.text}")
    data = response.json()
    return data['version']['number']

def move_page_to_parent_direct(page_id, new_parent_id, version):
    """
    Moves a Confluence page to a new parent by updating its ancestor.
    """
    url = f"{CONFLUENCE_URL}/wiki/rest/api/content/{page_id}"
    data = {
        "version": {"number": version + 1},
        "type": "page",
        "ancestors": [{"id": new_parent_id}]
    }
    return confluence_api_request("PUT", url, data)
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

    # 3. If no suitable page exists, create a new one using the direct API call.
    try:
        print(f"  Creating folder page: '{folder_title}' under parent ID {parent_id}")
        # Use the direct API call to create the folder page. Body is empty.
        created_page = create_page_direct(
            space=CONFLUENCE_SPACE_KEY,
            parent_id=parent_id,
            title=folder_title,
            storage_body="",
        )
        if created_page and created_page.get('id'):
            return created_page['id']
    except Exception as e:
        print(f"  (Error) create_page_direct call failed for '{folder_title}': {e}")
    
    # 4. As a final fallback, re-query using CQL to find the page we may have just created.
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
def main():
    """
    Executes the full synchronization process.
    """
    # 1. Initial Checks and Setup
    print("--- 1. Verifying Configuration ---")
    if not all([CONFLUENCE_URL, CONFLUENCE_USERNAME, CONFLUENCE_API_TOKEN, CONFLUENCE_SPACE_KEY, CONFLUENCE_PARENT_PAGE_ID]):
        print("FATAL: Missing required environment variables.")
        sys.exit(1)
    print(f"Syncing Markdown from '{DOCS_FOLDER}' to Confluence space '{CONFLUENCE_SPACE_KEY}' under parent '{CONFLUENCE_PARENT_PAGE_ID}'")

    # 2. Build folder hierarchy
    print("--- 2. Building Folder Hierarchy ---")
    folder_parent_ids = {"": CONFLUENCE_PARENT_PAGE_ID}
    if os.path.isdir(DOCS_FOLDER):
        for root, dirs, _ in os.walk(DOCS_FOLDER):
            rel_path = os.path.relpath(root, DOCS_FOLDER)
            folder_path = "" if rel_path == "." else rel_path.replace("\\", "/")
            parent_id = folder_parent_ids[folder_path]
            for d in sorted(dirs):
                sub_folder_path = os.path.join(folder_path, d).replace("\\", "/")
                if sub_folder_path not in folder_parent_ids:
                    folder_title = to_title(d)
                    folder_id = ensure_folder_page(folder_title, parent_id)
                    folder_parent_ids[sub_folder_path] = folder_id
    else:
        print(f"Warning: '{DOCS_FOLDER}' directory not found. No files will be processed.")

    # 3. Gather local markdown files
    print("--- 3. Scanning local Markdown files ---")
    local_markdown_pages = {}
    if os.path.isdir(DOCS_FOLDER):
        for root, _, files in os.walk(DOCS_FOLDER):
            rel_path = os.path.relpath(root, DOCS_FOLDER)
            folder_path = "" if rel_path == "." else rel_path.replace("\\", "/")
            parent_id = folder_parent_ids[folder_path]
            for file in sorted(files):
                if not file.endswith(".md"):
                    continue
                filepath = os.path.join(root, file)
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        content = f.read()
                except Exception as e:
                    print(f"Error reading file '{filepath}': {e}")
                    continue
                name_no_ext = os.path.splitext(file)[0]
                if name_no_ext.lower() == "index" and folder_path:
                    title = to_title(os.path.basename(folder_path))
                else:
                    title = to_title(name_no_ext)

                storage = markdown_to_storage(content)
                content_hash = md5(storage)
                key = (parent_id, title)

                local_markdown_pages[key] = {
                    "title": title,
                    "storage": storage,
                    "hash": content_hash,
                    "parent_id": parent_id,
                    "filepath": filepath,
                }
    print(f"Found {len(local_markdown_pages)} Markdown files locally.")

    # 4. Fetch existing Confluence pages
    print("--- 4. Fetching existing Confluence pages ---")
    confluence_pages = {}
    try:
        pages = confluence.get_all_pages_from_space(CONFLUENCE_SPACE_KEY, expand='ancestors,body.storage,version')
        for p in pages:
            parent_id = p['ancestors'][-1]['id'] if p.get('ancestors') else None
            storage_val = p.get('body', {}).get('storage', {}).get('value', '')
            key = (parent_id, p['title'])
            confluence_pages[key] = {
                "id": p['id'],
                "hash": md5(storage_val),
                "version": p['version']['number']
            }
        print(f"Found {len(confluence_pages)} pages on Confluence.")
    except Exception as e:
        print(f"Error fetching Confluence pages: {e}")
        sys.exit(1)

    # 5. Determine operations
    print("--- 5. Determining operations ---")
    to_create = []
    to_update = []
    for k, local_page in local_markdown_pages.items():
        remote_page = confluence_pages.get(k)
        if not remote_page:
            to_create.append(local_page)
        elif local_page['hash'] != remote_page['hash']:
            to_update.append({**local_page, **remote_page})

    archive_parent_id = ensure_archive_parent()
    managed_folder_ids = set(folder_parent_ids.values())

    to_archive = []
    for k, remote_page in confluence_pages.items():
        rid = str(remote_page['id'])
        if (k not in local_markdown_pages and 
            rid not in managed_folder_ids and 
            rid != str(archive_parent_id) and 
            rid != str(CONFLUENCE_PARENT_PAGE_ID)):
            archive_info = {**remote_page, "title": k[1], "parent_id": k[0]}
            to_archive.append(archive_info)

    # 6. Execute create
    print(f"Creating {len(to_create)} pages...")
    for page in to_create:
        try:
            create_page_direct(
                space=CONFLUENCE_SPACE_KEY,
                parent_id=page['parent_id'],
                title=page['title'],
                storage_body=page['storage'],
            )
            print(f"Created '{page['title']}'")
        except Exception as e:
            print(f"Failed to create '{page['title']}': {e}")

    # 7. Execute update
    print(f"Updating {len(to_update)} pages...")
    for page in to_update:
        try:
            update_page_direct(
                page_id=page['id'],
                title=page['title'],
                storage_body=page['storage'],
                version=page['version']
            )
            print(f"Updated '{page['title']}'")
        except Exception as e:
            print(f"Failed to update '{page['title']}': {e}")

    # 8. Execute archive (move to archive parent)
    print(f"Archiving {len(to_archive)} pages...")
    for page in to_archive:
        try:
            current_version = get_page_version(page['id'])
            move_page_to_parent_direct(
                page_id=page['id'],
                new_parent_id=archive_parent_id,
                version=current_version,
            )
            print(f"Archived '{page['title']}'")
        except Exception as e:
            print(f"Failed to archive '{page['title']}': {e}")

    # 9. Summary
    print("\nSync complete.")
    print(f"Created: {len(to_create)}, Updated: {len(to_update)}, Archived: {len(to_archive)}")

if __name__ == "__main__":
    main()
