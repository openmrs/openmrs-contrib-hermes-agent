"""Downloads the Maven binary distribution to /maven, checking it against its SHA-512.

Used by the maven build stage in this directory's Dockerfile; settings come from its build args.
"""

import hashlib
import io
import os
import tarfile
import urllib.request

version = os.environ["MAVEN_VERSION"]
expected = os.environ["MAVEN_SHA512"]
url = f"https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/{version}/apache-maven-{version}-bin.tar.gz"

data = urllib.request.urlopen(url, timeout=120).read()
actual = hashlib.sha512(data).hexdigest()
if actual != expected:
    raise SystemExit(f"{url}: sha512 {actual} != expected {expected}")
with tarfile.open(fileobj=io.BytesIO(data)) as archive:
    archive.extractall("/", filter="tar")
os.rename(f"/apache-maven-{version}", "/maven")
