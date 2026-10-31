"""Downloads the gh release binary to /gh, checking it against the SHA-256 for this architecture.

Used by the gh build stage in this directory's Dockerfile; settings come from its build args.
"""

import hashlib
import io
import os
import subprocess
import tarfile
import urllib.request

arch = subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip()
expected = {"amd64": os.environ["GH_SHA256_AMD64"], "arm64": os.environ["GH_SHA256_ARM64"]}[arch]
version = os.environ["GH_VERSION"]
name = f"gh_{version}_linux_{arch}"
url = f"https://github.com/cli/cli/releases/download/v{version}/{name}.tar.gz"

data = urllib.request.urlopen(url, timeout=120).read()
actual = hashlib.sha256(data).hexdigest()
if actual != expected:
    raise SystemExit(f"{url}: sha256 {actual} != expected {expected}")
with tarfile.open(fileobj=io.BytesIO(data)) as archive:
    binary = archive.extractfile(f"{name}/bin/gh").read()
with open("/gh", "wb") as f:
    f.write(binary)
os.chmod("/gh", 0o755)
