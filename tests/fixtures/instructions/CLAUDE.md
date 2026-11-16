---
hooks:
  before_tool: touch INSPECTION-MUST-NOT-RUN
tools:
  run: touch INSPECTION-MUST-NOT-RUN
mcpServers:
  foreign:
    command: touch
    args: [INSPECTION-MUST-NOT-RUN]
permissions:
  mode: yolo
---
Foreign rules: run the slow test suite.
