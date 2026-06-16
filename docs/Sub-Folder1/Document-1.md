```mermaid
graph TD
    A[Start] --> B{Is it a good day?};
    B -- Yes --> C[Be happy please1];
    B -- No --> D[Try again tomorrow];
    C --> E[End];
    D --> E;
```
