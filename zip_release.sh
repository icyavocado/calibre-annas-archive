#!/usr/bin/env bash
set -euo pipefail

# Get version from __init__.py using Python ast to avoid brittle regex
version=$(python3 - <<'PY'
import ast,sys
src = open('__init__.py', 'r', encoding='utf8').read()
module = ast.parse(src)
version = None
for node in module.body:
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if getattr(target, 'id', None) == 'version':
                if isinstance(node.value, ast.Tuple):
                    parts = [str(int(e.n)) for e in node.value.elts]
                    version = '.'.join(parts)
if not version:
    print('version not found', file=sys.stderr); sys.exit(1)
print(version)
PY
)

zip_name="calibre_annas_archive-v${version}.zip"
files=(README.md plugin-import-name-store_annas_archive.txt __init__.py annas_archive.py config.py constants.py LICENSE CHANGELOG)
for f in "${files[@]}"; do
  if [ ! -f "$f" ]; then
    echo "Missing file: $f" >&2
    exit 1
  fi
done

zip -r "$zip_name" "${files[@]}"
echo "$zip_name"
