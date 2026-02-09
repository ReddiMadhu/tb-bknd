"""
Tabular Editor - Automatic Installer for Office Laptop
No admin privileges required - installs to user directory
"""
import urllib.request
import zipfile
import os
from pathlib import Path
import sys


def check_existing_installation():
    """Check if Tabular Editor is already installed"""

    search_paths = [
        Path(r"C:\Program Files (x86)\Tabular Editor\TabularEditor.exe"),
        Path(r"C:\Program Files\Tabular Editor\TabularEditor.exe"),
        Path.home() / "AppData/Local/TabularEditor/TabularEditor.exe",
        Path("./tools/TabularEditor/TabularEditor.exe"),
    ]

    for path in search_paths:
        if path.exists():
            print(f"✅ Found existing installation: {path}")
            return path

    return None


def install_tabular_editor():
    """Download and install Tabular Editor to user directory"""

    print("=" * 70)
    print("Tabular Editor 2 - Automatic Installation")
    print("Installing to user directory (no admin needed)")
    print("=" * 70)
    print()

    # Check if already installed
    existing = check_existing_installation()
    if existing:
        print("✅ Tabular Editor is already installed!")
        return existing

    # Install to user directory
    install_dir = Path.home() / "TabularEditor"
    download_url = "https://github.com/TabularEditor/TabularEditor/releases/latest/download/TabularEditor.Portable.zip"

    print(f"📁 Installation directory: {install_dir}")
    print(f"🌐 Download URL: {download_url}")
    print()

    # Create directory
    install_dir.mkdir(parents=True, exist_ok=True)
    print(f"✓ Created directory: {install_dir}")

    # Download
    zip_path = install_dir / "TabularEditor.zip"

    try:
        print("⬇️  Downloading Tabular Editor... (this may take 1-2 minutes)")

        def show_progress(block_num, block_size, total_size):
            downloaded = block_num * block_size
            if total_size > 0:
                percent = min(downloaded * 100 / total_size, 100)
                bar_length = 50
                filled_length = int(bar_length * percent / 100)
                bar = '█' * filled_length + '-' * (bar_length - filled_length)
                print(f'\r[{bar}] {percent:.1f}%', end='', flush=True)

        urllib.request.urlretrieve(download_url, zip_path, show_progress)
        print()  # New line after progress
        print("✓ Download complete!")

    except Exception as e:
        print(f"❌ Download failed: {e}")
        print("\nManual installation:")
        print(f"1. Visit: {download_url}")
        print(f"2. Extract to: {install_dir}")
        return None

    # Extract
    try:
        print("📦 Extracting files...")
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(install_dir)

        zip_path.unlink()  # Remove zip
        print("✓ Extraction complete!")

    except Exception as e:
        print(f"❌ Extraction failed: {e}")
        return None

    # Verify
    exe_path = install_dir / "TabularEditor.exe"
    if exe_path.exists():
        print()
        print("=" * 70)
        print("✅ INSTALLATION SUCCESSFUL!")
        print("=" * 70)
        print()
        print(f"📍 Installed at: {exe_path}")
        print()
        return exe_path
    else:
        print("❌ Installation failed - executable not found")
        return None


def update_env_file(exe_path):
    """Update .env file with Tabular Editor path"""

    env_file = Path(".env")

    # Read existing .env or create new
    if env_file.exists():
        with open(env_file, 'r') as f:
            lines = f.readlines()
    else:
        lines = []

    # Check if TABULAR_EDITOR_PATH already exists
    updated = False
    for i, line in enumerate(lines):
        if line.startswith("TABULAR_EDITOR_PATH="):
            lines[i] = f'TABULAR_EDITOR_PATH={exe_path}\n'
            updated = True
            break

    # Add if not exists
    if not updated:
        lines.append(f'\n# Tabular Editor\nTABULAR_EDITOR_PATH={exe_path}\n')

    # Write back
    env_file.parent.mkdir(parents=True, exist_ok=True)
    with open(env_file, 'w') as f:
        f.writelines(lines)

    print(f"✓ Updated .env file: {env_file}")


def test_installation(exe_path):
    """Test if Tabular Editor works"""

    print("\n🧪 Testing installation...")

    try:
        import subprocess
        result = subprocess.run(
            [str(exe_path), "/?"],
            capture_output=True,
            text=True,
            timeout=5
        )

        if "Tabular Editor" in result.stdout or "Tabular Editor" in result.stderr:
            print("✅ Tabular Editor is working correctly!")
            return True
        else:
            print("⚠️  Tabular Editor may not be working properly")
            return False

    except Exception as e:
        print(f"⚠️  Could not test installation: {e}")
        return False


if __name__ == "__main__":
    print()
    print("This script will install Tabular Editor 2 to your user directory")
    print("No administrator privileges required!")
    print()

    # Install
    exe_path = install_tabular_editor()

    if exe_path:
        # Update .env file
        print()
        update_env_file(exe_path)

        # Test installation
        test_installation(exe_path)

        print()
        print("=" * 70)
        print("🎉 ALL DONE!")
        print("=" * 70)
        print()
        print("Next steps:")
        print("1. The migration system will automatically detect Tabular Editor")
        print("2. PBIX files will be generated during migration")
        print("3. No additional configuration needed!")
        print()

        sys.exit(0)
    else:
        print()
        print("=" * 70)
        print("❌ Installation failed")
        print("=" * 70)
        print()
        print("Manual installation:")
        print("1. Download from: https://github.com/TabularEditor/TabularEditor/releases")
        print("2. Extract to any folder")
        print("3. Add path to bknd/.env file:")
        print(f"   TABULAR_EDITOR_PATH=C:\\path\\to\\TabularEditor.exe")
        print()

        sys.exit(1)
