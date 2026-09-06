"""Build MiyeePDFSigner.exe - a single file that needs nothing installed.

Run on Windows with PyInstaller present:

    pip install pyinstaller PyKCS11 asn1crypto cryptography
    python build_exe.py

The result is dist/MiyeePDFSigner.exe. It is not code-signed, so Windows
warns the first time it runs; signing it is a separate step that needs a
code-signing certificate.
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENTRY = os.path.join(HERE, "miyee_signer.py")


def main():
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        sys.exit("PyInstaller is not installed. Run: pip install pyinstaller")

    args = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--name", "MiyeePDFSigner",
        # A console window, on purpose: it is where the helper says which
        # driver it found and which token it can see, which is most of what
        # anyone needs when a token is not being detected.
        "--console",
        # tkinter draws the PIN prompt; PyInstaller finds it, but these are
        # the pieces it has been known to miss.
        "--hidden-import", "tkinter",
        "--hidden-import", "tkinter.simpledialog",
        "--hidden-import", "tkinter.messagebox",
        "--hidden-import", "PyKCS11",
        "--hidden-import", "asn1crypto.cms",
        "--hidden-import", "cryptography.x509",
        "--distpath", os.path.join(HERE, "dist"),
        "--workpath", os.path.join(HERE, "build"),
        "--specpath", os.path.join(HERE, "build"),
        ENTRY,
    ]
    icon = os.path.join(HERE, "icon.ico")
    if os.path.exists(icon):
        args[3:3] = ["--icon", icon]

    print(" ".join(args))
    raise SystemExit(subprocess.call(args))


if __name__ == "__main__":
    main()
