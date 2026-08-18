# Responsibility: Prove the built wheel resolves from site-packages with its package data.
# Boundaries: stdlib-only and import-safe: it runs inside the image build before anything else is trusted.
from __future__ import annotations

import pathlib
import site
import sys

import meshpipeline

pkg = pathlib.Path(meshpipeline.__file__).resolve().parent

# 1. It resolves from an installed location. (Debian/Ubuntu's system python uses
#    dist-packages; a venv uses site-packages - accept either, reject a checkout.)
install_roots = [pathlib.Path(p).resolve() for p in (site.getsitepackages() + [site.getusersitepackages()])]
if not any(root in pkg.parents for root in install_roots):
    sys.exit(f"FAIL: meshpipeline resolves from {pkg}, which is not an install root "
             f"({[str(r) for r in install_roots]}) - the image is importing a source tree")

# 2. No checkout path is needed (or present) to import it.
if "/srv/src" in str(pkg) or "PYTHONPATH" in [k for k in ("PYTHONPATH",) if __import__("os").environ.get(k)]:
    sys.exit(f"FAIL: the image still leans on a checkout path (pkg={pkg}, "
             f"PYTHONPATH={__import__('os').environ.get('PYTHONPATH')!r})")

# 3. The package DATA came with the wheel - the app cannot load its prompts without it, and a
#    wheel that ships only .py fails at first use, not at build.
#    The prompt list is DERIVED from the installed settings/policy.py's REQUIRED_PROMPTS -
#    the app's own declaration of what it refuses to start without - parsed textually so this
#    stays stdlib-only and import-safe at build time. A hardcoded copy would fail the build on
#    files nothing needs, the first time a prompt is retired.
import re

policy_src = (pkg / "settings" / "policy.py").read_text(encoding="utf-8")
block = re.search(r"REQUIRED_PROMPTS[^=]*=\s*\[(.*?)\]", policy_src, re.S)
if not block:
    sys.exit("FAIL: could not locate REQUIRED_PROMPTS in the installed settings/policy.py - "
             "the wheel is broken or the declaration moved; update deploy/verify_install.py")
prompt_files = re.findall(r'\(\s*"[^"]+"\s*,\s*"([^"]+)"\s*\)', block.group(1))
if not prompt_files:
    sys.exit("FAIL: REQUIRED_PROMPTS parsed empty - the declaration changed shape; "
             "update deploy/verify_install.py")

# Prompts are the only PACKAGE DATA the wheel must carry. Review presentation is ordinary
# package CODE (render/review_palette.py), so it needs no package-data assertion here; if it
# ever fails to ship, importing it fails loudly on its own.
for rel in [f"prompts/{f}" for f in prompt_files]:
    if not (pkg / rel).is_file():
        sys.exit(f"FAIL: the installed wheel is missing package data: {rel}")

print(f"OK: meshpipeline installed at {pkg} (version {getattr(meshpipeline, '__version__', 'n/a')}), "
      f"package data present, no checkout path needed")
