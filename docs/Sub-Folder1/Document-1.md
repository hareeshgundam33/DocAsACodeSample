### My Awesome Diagram
```mermaid
graph TD
    A[Start] --> B(Sync Code);
    B --> C{Success?};
    C -->|Yes| D[Check onces];
    C -->|No| F[Check Logs];
