# Sample Document with a Mermaid Diagram 2

This is a sample markdown document to demonstrate how to embed a Mermaid diagram.

## Mermaid Flowchart Example

Here's a simple flowchart:
```mermaid
graph TD
    A[Start] --> B{Is it a good day?};
    B -- Yes --> C[Be happy please1];
    B -- No --> D[Try again tomorrow];
    C --> E[End];
    D --> E;
