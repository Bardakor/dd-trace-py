import os
import site
import sys


dependency_path = os.environ.get("DD_TEST_SITE_PACKAGES")
if dependency_path:
    site.addsitedir(dependency_path)
    sys.path.remove(dependency_path)
    sys.path.insert(0, dependency_path)
