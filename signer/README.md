# MiyeePDF Signer

A small program that lets MiyeePDF sign a PDF with the DSC on your USB token.

## Why it is needed

A browser cannot talk to a USB signing token. There is no PKCS#11 support in
any browser, and the operating system's smart-card service holds the device,
so nothing in a web page can reach it. Every online signing service in India
works around this the same way: a small program on the computer that owns the
token, which the page asks to sign on its behalf. This is that program.

## What is sent to it

**A 32-byte hash of the document, and nothing else.** The PDF stays in your
browser. MiyeePDF works out exactly which bytes the signature has to cover,
hashes them in the page, and sends only that hash here. The finished signature
comes back and is slipped into the file, still in the browser.

Your PIN is typed into a window this program opens, and goes straight to the
token's driver. It never travels over the connection, is never seen by the web
page, and is not kept after the signature is made. The private key never
leaves the token: the token itself does the signing.

The helper listens on `127.0.0.1` only — it is not reachable from anywhere but
your own machine — and answers only pages served from `vipinnairv.github.io`
(or a local build on port 8200). Every signature asks you first, naming the
document.

## Install

You need Python 3.9 or newer, plus your token's driver installed (the software
that came with the DSC — ePass 2003, ProxKey, Watchdata, SafeNet and so on).

```
pip install PyKCS11 asn1crypto cryptography
python miyee_signer.py
```

On Windows a ready-made `MiyeePDFSigner.exe` can be built with
`python build_exe.py` (see below), so nothing else has to be installed.

## Use

1. Plug the token in.
2. Start the helper. It prints the driver it found and the token it can see.
3. Open MiyeePDF, go to **Fill & Sign**, and in the **Digital signature (DSC)**
   panel press **Look again**. Your certificate appears in the list.
4. Drag a box on the page where the signature should appear, then press
   **Sign digitally & save**.
5. The helper asks whether to sign that document, and for the token PIN.

MiyeePDF does not go looking for the helper until you press *Look again* that
first time. From then on it checks by itself whenever you open a document, so
the token is already listed by the time you reach the panel.

## If it cannot find your token

The helper looks for the usual drivers by itself. If yours is somewhere else,
point at it:

```
python miyee_signer.py --module "C:\Windows\System32\eps2003csp11.dll"
```

Common driver files:

| Token | Driver |
|---|---|
| ePass 2003 (many Indian CAs) | `C:\Windows\System32\eps2003csp11.dll` |
| ProxKey | `C:\Windows\System32\SignatureP11.dll` |
| Watchdata / WD ProxKey | `C:\Windows\System32\WDPKCS.dll` |
| SafeNet eToken | `C:\Windows\System32\eTPKCS11.dll` |
| Aladdin | `C:\Windows\System32\aetpkss1.dll` |
| Trustkey | `C:\Windows\System32\ShuttleCsp11_3003.dll` |
| macOS (OpenSC) | `/Library/OpenSC/lib/opensc-pkcs11.so` |
| Linux (OpenSC) | `/usr/lib/x86_64-linux-gnu/opensc-pkcs11.so` |

Other options:

```
--port 8787          listen somewhere else (tell the page too)
--origin https://…   allow another page to use the helper
```

## Building the Windows executable

```
pip install pyinstaller PyKCS11 asn1crypto cryptography
python build_exe.py
```

The result is `dist/MiyeePDFSigner.exe`, a single file that needs nothing
installed. It is not code-signed, so Windows SmartScreen will warn the first
time; that warning is about this executable being new, not about the token.

## Which browsers can reach the helper

The page is served over HTTPS and the helper over plain HTTP on
`127.0.0.1`. Browsers treat loopback as trustworthy, so this is allowed — but
not everywhere:

| Browser | Works |
|---|---|
| Chrome, Edge, Brave and other Chromium browsers | yes, tested |
| Firefox | expected to work — it treats loopback as trustworthy too, but this has not been tested |
| Safari | no — it does not let a page reach `127.0.0.1` |

On Safari, use the **Certificate file** tab instead.

## Checking the signature

The signed file is an ordinary PDF with a detached PKCS#7 signature
(`adbe.pkcs7.detached`), which is what Adobe Acrobat, Foxit and DSC verifiers
read. Acrobat will show it as valid but may say the identity is not trusted
until your CA's root is in its trust list — that is Acrobat's trust setting,
not a problem with the signature.

## Troubleshooting

**"No signing helper is running on this computer."** The helper is not started,
or it is listening on a different port. Start it and press *Look again*.

**"The helper is running but no token is plugged in."** The driver loaded but
reports no token. Re-seat the USB token; some drivers need it in before they
are loaded, so restart the helper after plugging it in.

**"That PIN is not right."** The token counts wrong attempts and locks after a
few. Unlock it with the software that came with it — this helper cannot.

**Nothing happens after pressing Sign.** The PIN window may be behind the
browser. Look for it in the taskbar.
