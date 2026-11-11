import subprocess
import sys
import time
from pathlib import Path

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
Path("child.pid").write_text(str(child.pid))
marker = 1  # breakpoint after the child exists
while True:
    time.sleep(300)
