import os
import sys
import hashlib
import re
from datetime import datetime, timezone
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

# Set FORCE_UPDATE=true env var to re-push all pages regardless of hash
FORCE_UPDATE = os.environ.get('FORCE_UPDATE', 'false').lower() == 'true'

# --- Mermaid macro name ---
# Different Confluence Mermaid apps use different macro names.
# Set MERMAID_MACRO_NAME in your GitHub Actions env to switch between them.
# Common values:
#   "mermaid"                        -> Mermaid Diagrams for Confluence (Stratus Add-ons)
#   "mermaid-cloud"                  -> Mermaid Charts & Diagrams for Confluence (weweave)
#   "mermaid-vt"                     -> Mermaid for Confluence (Tech Labs)
#   "mermaidjs"                      -> Some other vendors
# To find the exact name: Edit a Confluence page -> Insert Mermaid macro ->
# Save -> open page URL with ?view=storage -> look for ac:name="..."
MERMAID_MACRO_NAME = os.environ.get('MERMAID_MACRO_NAME', 'mermaid-cloud')

confluence = Confluence(
    url=CONFLUENCE_URL,
    username=CONFLUENCE_USERNAME,
    password=CONFLUENCE_API_TOKEN,
    cloud=True,
)


def md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def to_title(name: str) -> str:
    return name.replace("-", " ").replace("_", " ").strip().title()


def normalize_line_endings(text: str) -> str:
    return text.replace('\r\n', '\n').replace('\r', '\n')


def mermaid_code_to_confluence_macro(diagram_code: str) -> str:
    """
    Converts mermaid diagram code into a Confluence storage format macro.
    - Strips trailing semicolons from each line (fixes rendering issues)
    - Strips leading/trailing whitespace
    - Uses MERMAID_MACRO_NAME env var to control which app macro is used
    """
    cleaned_lines = [line.rstrip(';') for line in diagram_code.splitlines()]
    cleaned_code = '\n'.join(cleaned_lines).strip()
    print(f"  [DEBUG] Using macro name: '{MERMAID_MACRO_NAME}'")
    print(f"  [DEBUG] Cleaned mermaid code:\n{cleaned_code}")
    return (
        f'<ac:structured-macro ac:name="{MERMAID_MACRO_NAME}" ac:schema-version="1">'
        f'<ac:plain-text-body>'
        f'<![CDATA[{cleaned_code}]]>'
        f'</ac:plain-text-body>'
        f'</ac:structured-macro>'
    )


def replace_mermaid_with_placeholders(md_content: str):
    """
    STEP 1: Extract all mermaid blocks BEFORE markdown parsing.
    Replace each block with a unique safe placeholder string.

    Handles ALL fence styles:
    - Backtick fence:     ```mermaid   (most common)
    - Single quote fence: '''mermaid
    - Tilde fence:        ~~~mermaid
    - With spaces:        ``` mermaid
    - Case insensitive:   ```MERMAID

    Returns (modified_content, {placeholder_key: diagram_code})
    """
    # Universal pattern: matches ```, ''', or ~~~ (3 or more of each)
    universal_pattern = re.compile(
        r'(?:^|\n)[ \t]*(?:`{3,}|~{3,}|\'{3,})[ \t]*[Mm][Ee][Rr][Mm][Aa][Ii][Dd][ \t]*\n'
        r'(.*?)\n'
        r'[ \t]*(?:`{3,}|~{3,}|\'{3,})[ \t]*(?=\n|$)',
        re.DOTALL
    )

    placeholders = {}
    counter = [0]

    def replacer(match):
        key = f'MERMAID_PLACEHOLDER_{counter[0]}'
        diagram_code = match.group(1)
        placeholders[key] = diagram_code
        counter[0] += 1
        print(f"  [DEBUG] Matched mermaid block {counter[0] - 1}. "
              f"First 80 chars: {diagram_code[:80]!r}")
        return f'\n\nMERMAID_PLACEHOLDER_{counter[0] - 1}\n\n'

    modified = universal_pattern.sub(replacer, md_content)

    print(f"  [DEBUG] Found {len(placeholders)} mermaid block(s) in content.")

    if len(placeholders) == 0 and 'mermaid' in md_content.lower():
        print("  ⚠️  WARNING: 'mermaid' keyword found but NO block matched any fence pattern!")
        print("  ⚠️  Printing lines around 'mermaid' for diagnosis:")
        lines = md_content.splitlines()
        for i, line in enumerate(lines):
            if 'mermaid' in line.lower():
                print(f"    Line {i + 1}: {line!r}")
                print(f"    Hex    : {line.encode('utf-8').hex(' ')}")

    return modified, placeholders


def restore_mermaid_macros(html_content: str, placeholders: dict) -> str:
    """
    STEP 3: Replace each placeholder in the HTML with the Confluence mermaid macro.
    Handles both <p>PLACEHOLDER</p> and bare PLACEHOLDER cases.
    """
    for key, diagram_code in placeholders.items():
        macro = mermaid_code_to_confluence_macro(diagram_code)

        # Replace <p>KEY</p> — markdown wraps bare text in <p> tags
        new_html = re.sub(
            rf'<p>\s*{re.escape(key)}\s*</p>',
            macro,
            html_content
        )

        if new_html != html_content:
            print(f"  [DEBUG] Replaced <p>{key}</p> with macro. ✅")
            html_content = new_html
        elif key in html_content:
            # Fallback: replace bare KEY if not wrapped in <p>
            html_content = html_content.replace(key, macro)
            print(f"  [DEBUG] Replaced bare {key} with macro. ✅")
        else:
            print(f"  ⚠️  WARNING: Could not find {key} anywhere in HTML to replace!")

    return html_content


def markdown_to_storage(md_content: str) -> str:
    """
    Converts Markdown to Confluence storage format using a safe 3-step approach:

    STEP 1 — Extract mermaid blocks, replace with safe placeholders
    STEP 2 — Convert remaining markdown to HTML (placeholders pass through untouched)
    STEP 3 — Replace placeholders in HTML with Confluence mermaid macros
    """
    # Normalize line endings (handles Windows CRLF from Git)
    md_content = normalize_line_endings(md_content)

    # STEP 1
    md_with_placeholders, placeholders = replace_mermaid_with_placeholders(md_content)

    # STEP 2
    html = markdown.markdown(
        md_with_placeholders,
        extensions=['fenced_code', 'tables', 'toc', 'codehilite']
    )

    # STEP 3
    if placeholders:
        html = restore_mermaid_macros(html, placeholders)

    # Verify all placeholders were successfully replaced
    for key in placeholders:
        if key in html:
            print(f"  ⚠️  WARNING: Placeholder '{key}' was NOT replaced in output HTML!")

    combined = f'<div class="markdown-body">{html}</div>'

    if placeholders and f'ac:name="{MERMAID_MACRO_NAME}"' not in combined:
        print("  ⚠️  WARNING: Mermaid macro NOT found in final storage output!")
    elif placeholders:
        print("  ✅ Mermaid macro successfully injected into storage output.")

    return combined


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
    # 1) CQL search under parent
    try:
        cql = f'title = "{folder_title}" AND ancestor = {parent_id} AND type = page'
        res = confluence.cql(cql, limit=1, expand='content.id,content.title')
        if res and res.get('results'):
            return res['results'][0]['content']['id']
    except Exception:
        pass

    # 2) Check anywhere in space
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

    # 3) Create under requested parent
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

    # 4) CQL fallback
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

    raise RuntimeError(
        f"Unable to ensure or locate folder page '{folder_title}' under parent {parent_id}"
    )


def ensure_archive_parent() -> str:
    if CONFLUENCE_ARCHIVE_PARENT_PAGE_ID:
        try:
            page = confluence.get_page_by_id(CONFLUENCE_ARCHIVE_PARENT_PAGE_ID, expand='id')
            if page and page.get('id'):
                return page['id']
        except Exception:
            print(
                f"Warning: CONFLUENCE_ARCHIVE_PARENT_PAGE_ID not found: "
                f"{CONFLUENCE_ARCHIVE_PARENT_PAGE_ID}"
            )
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

    if FORCE_UPDATE:
        print("⚠️  FORCE_UPDATE=true: All pages will be re-pushed regardless of content hash.")

    print(f"ℹ️   Using Mermaid macro name: '{MERMAID_MACRO_NAME}'")
    print(
        f"Starting sync: Markdown files from '{DOCS_FOLDER}' to Confluence space "
        f"'{CONFLUENCE_SPACE_KEY}' under parent page ID '{CONFLUENCE_PARENT_PAGE_ID}'."
    )

    # --- 2. Build Confluence Folder Hierarchy ---
    folder_parent_ids = {"": CONFLUENCE_PARENT_PAGE_ID}

    if os.path.isdir(DOCS_FOLDER):
        for root, dirs, files in os.walk(DOCS_FOLDER):
            rel = os.path.relpath(root, DOCS_FOLDER)
            folder_path = "" if rel == "." else rel.replace("\\", "/")
            current_confluence_parent_id = folder_parent_ids[folder_path]

            for d in sorted(dirs):
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

                print(f"\nProcessing file: {filepath} -> title: '{title}'")

                # 3-step mermaid-safe conversion
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

    # --- 4. Fetch ALL existing pages in Confluence ---
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

    # --- 5. Determine Actions ---
    pages_to_create = []
    pages_to_update_or_move = []
    pages_to_archive = []

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
        needs_update = FORCE_UPDATE or (local_info['hash'] != remote_info.get('hash'))

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
    try:
        archive_parent_page_id = ensure_archive_parent()
    except Exception as e:
        print(f"Error ensuring archive parent: {e}")
        sys.exit(1)

    # Create new pages and update
    for p in pages_to_create:
        print(f"\nCreating page '{p['title']}' under parent {p['parent_id']} from {p['filepath']}.")
        if f'ac:name="{MERMAID_MACRO_NAME}"' in p['storage']:
            macro_start = p['storage'].find('<ac:structured-macro')
            print(f"  [DEBUG] Mermaid macro in storage:\n  {p['storage'][macro_start:macro_start + 300]}")
        try:
            confluence.create_page(
                space=CONFLUENCE_SPACE_KEY,
                parent_id=p["parent_id"],
                title=p["title"],
                body=p["storage"],
                representation="storage",
            )
            print(f"✅ Successfully created '{p['title']}'.")
        except Exception as e:
            print(f"❌ Error creating page '{p['title']}': {e}")

    # Update or move existing pages
    for p in pages_to_update_or_move:
        move_desc = (
            f"moving from parent {p['current_parent_id']} to {p['target_parent_id']}"
            if str(p['current_parent_id']) != str(p['target_parent_id'])
            else "updating content"
        )
        print(f"\nProcessing page '{p['title']}' (ID {p['id']}): {move_desc} from {p['filepath']}.")
        if f'ac:name="{MERMAID_MACRO_NAME}"' in p['storage']:
            macro_start = p['storage'].find('<ac:structured-macro')
            print(f"  [DEBUG] Mermaid macro in storage:\n  {p['storage'][macro_start:macro_start + 300]}")
        try:
            confluence.update_page(
                page_id=p["id"],
                title=p["title"],
                body=p["storage"],
                parent_id=p["target_parent_id"],
            )
            print(f"✅ Successfully updated/moved '{p['title']}'.")
        except Exception as e:
            print(f"❌ Error updating/moving page '{p['title']}' (ID {p['id']}): {e}")

    # Archive pages
    archived_count = 0
    for p in pages_to_archive:
        print(f"\nArchiving page '{p['title']}' (ID {p['id']}) — no longer in Git repo.")
        try:
            original_parent = p.get('parent_id') or "Unknown"
            timestamp = datetime.now(timezone.utc).isoformat()
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
            print(f"✅ Successfully archived '{p['title']}' (ID {archive_parent_page_id}).")
        except Exception as e:
            print(f"❌ Error archiving page '{p['title']}' (ID {p['id']}): {e}")

    # --- 7. Summary ---
    print("\n========== Sync Summary ==========")
    print(f"Pages created  : {len(pages_to_create)}")
    print(f"Pages updated  : {len(pages_to_update_or_move)}")
    print(f"Pages archived : {archived_count}")
    print(f"Force update   : {FORCE_UPDATE}")
    print(f"Mermaid macro  : {MERMAID_MACRO_NAME}")
    print("===================================")
    print("Sync complete.")


if __name__ == "__main__":
    main()

