"""Normal Windows source entry point; share WT's maintained native host."""
import os
from pathlib import Path
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    os.chdir(root)
    # Production permissions/data remain production; this is NOT WT mode.
    os.environ['JUPITER_ENV'] = 'production'
    os.environ['JUPITER_RUNTIME_ID'] = 'windows-source'
    os.environ['JUPITER_SOURCE_LAUNCH'] = 'true'
    # Retain the existing source WebView profile; never clear user data.
    os.environ.setdefault('JUPITER_WEBVIEW_DATA_DIR',
                          os.path.join(os.getenv('LOCALAPPDATA', ''), 'JupiterWT_Data'))
    import build_app
    build_app.main()


if __name__ == '__main__':
    main()
