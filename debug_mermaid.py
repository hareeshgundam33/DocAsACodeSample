import re

# Paste the exact content of your markdown file here
# OR point this script to your actual file
MARKDOWN_FILE = 'docs/Document-1.md'  # <-- change this to your actual file path

with open(MARKDOWN_FILE, 'r', encoding='utf-8') as f:
    content = f.read()

print('=== RAW BYTES (first 500) ===')
print(repr(content[:500]))
print()

# Check what kind of line endings the file uses
if '\r\n' in content:
    print('Line endings: Windows (CRLF - \\r\\n)')
elif '\r' in content:
    print('Line endings: Old Mac (CR - \\r)')
else:
    print('Line endings: Unix (LF - \\n)')
print()

# Try to find the mermaid block
patterns = [
    ('Strict (LF only)',          r'```mermaid\n(.*?)\n```'),
    ('Flexible (spaces + LF)',    r'```mermaid\s*\n(.*?)\n\s*```'),
    ('Very loose (any whitespace)', r'```mermaid[\s\S]*?```'),
]

for label, pattern in patterns:
    matches = re.findall(pattern, content, re.DOTALL)
    print(f'Pattern [{label}]: {len(matches)} match(es) found')
    for m in matches:
        print(f'  -> Captured: {repr(m[:80])}')
print()

# Check if the backticks in the file are real backticks (ASCII 96)
backtick_positions = [i for i, c in enumerate(content) if c == '`']
print(f'Backtick character (`) found at positions: {backtick_positions[:10]}')
print()

# Show characters around the mermaid keyword
idx = content.find('mermaid')
if idx != -1:
    surrounding = content[max(0, idx-5):idx+20]
    print(f'Characters around mermaid keyword: {repr(surrounding)}')
else:
    print('The word mermaid was NOT found in the file at all!')
