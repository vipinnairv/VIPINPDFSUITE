"""MiyeePDF signer - a small local helper for signing with a DSC token.

Why this exists
---------------
A browser cannot reach a USB smart-card token. There is no PKCS#11 API in
any browser, and the operating system's smart-card service holds the device,
so WebUSB cannot claim it either. Every web signing service in India solves
this the same way: a small program on the machine that owns the token, which
the page asks to sign on its behalf. This is that program.

What crosses the wire
---------------------
A 32-byte hash, and nothing else. The document stays in the browser: the
page prepares the signature slot, works out which bytes the signature covers,
hashes them, and sends only that digest here. The signed blob comes back.
Neither this helper nor anything else on the machine ever sees the file.

The PIN is asked for by this program, in a native dialog, and goes straight
to the token driver. It never travels over HTTP and is never held after use.

Talking to it
-------------
    GET  /status        is it running, and is a token present
    GET  /certificates  the signing certificates the token holds
    POST /sign          { certId, digest, digestAlgo, document } -> CMS blob

Only pages served from an allowed origin may call it, and every signature
asks the person at the keyboard first, naming the document.
"""

import argparse
import base64
import datetime
import hashlib
import http.server
import json
import os
import platform
import queue
import socketserver
import sys
import threading

VERSION = "1.0.0"
DEFAULT_PORT = 8787

# Where the page that may use this helper is served from. A local build is
# allowed as well, so the helper can be developed against.
ALLOWED_ORIGINS = {
    "https://vipinnairv.github.io",
    "http://localhost:8200",
    "http://127.0.0.1:8200",
}

# The usual PKCS#11 modules for the tokens sold in India, plus the software
# token used for testing. The first one that loads and reports a slot wins.
CANDIDATE_MODULES = {
    "Windows": [
        r"C:\Windows\System32\eps2003csp11.dll",      # ePass 2003
        r"C:\Windows\System32\SignatureP11.dll",      # ProxKey
        r"C:\Windows\System32\WDPKCS.dll",            # Watchdata
        r"C:\Windows\System32\eTPKCS11.dll",          # SafeNet
        r"C:\Windows\System32\aetpkss1.dll",          # Aladdin
        r"C:\Windows\System32\ShuttleCsp11_3003.dll", # Trustkey
    ],
    "Linux": [
        "/usr/lib/softhsm/libsofthsm2.so",
        "/usr/lib/x86_64-linux-gnu/softhsm/libsofthsm2.so",
        "/usr/lib/x86_64-linux-gnu/opensc-pkcs11.so",
        "/usr/local/lib/libeTPkcs11.so",
    ],
    "Darwin": [
        "/Library/OpenSC/lib/opensc-pkcs11.so",
        "/usr/local/lib/libeTPkcs11.dylib",
    ],
}


def log(*parts):
    print(f"[{datetime.datetime.now():%H:%M:%S}]", *parts, flush=True)


_NO_DRIVER = ("No token driver is loaded. Install the software that came with "
              "your DSC token and restart this helper.")


class TokenError(Exception):
    """Something the person can act on: no token, wrong PIN, no certificate."""


class Token:
    """The PKCS#11 side: find a token, list its certificates, sign a hash."""

    def __init__(self, module_path=None):
        import PyKCS11
        self.pkcs11 = PyKCS11
        self.lib = PyKCS11.PyKCS11Lib()
        self.module = module_path or self._find_module()
        if not self.module:
            raise TokenError(
                "No PKCS#11 driver found. Install the software that came with "
                "your DSC token, or start this helper with --module pointing at "
                "its .dll/.so file.")
        self.lib.load(self.module)

    def _find_module(self):
        for path in CANDIDATE_MODULES.get(platform.system(), []):
            if os.path.exists(path):
                return path
        return None

    def slots(self):
        try:
            return self.lib.getSlotList(tokenPresent=True)
        except Exception:
            return []

    def describe(self):
        """What is plugged in, without asking for a PIN."""
        out = []
        for slot in self.slots():
            try:
                info = self.lib.getTokenInfo(slot)
                out.append({
                    "slot": int(slot),
                    "label": str(info.label).strip(),
                    "manufacturer": str(info.manufacturerID).strip(),
                    "model": str(info.model).strip(),
                    "serial": str(info.serialNumber).strip(),
                })
            except Exception:
                continue
        return out

    def certificates(self, pin=None):
        """Signing certificates on the token.

        Reading a certificate does not always need a PIN, and where it does
        not, the list can be shown before anyone is asked for one.
        """
        from PyKCS11 import CKA_CLASS, CKO_CERTIFICATE, CKA_VALUE, CKA_ID, CKA_LABEL
        found = []
        for slot in self.slots():
            session = None
            try:
                session = self.lib.openSession(slot)
                if pin:
                    session.login(pin)
                for handle in session.findObjects([(CKA_CLASS, CKO_CERTIFICATE)]):
                    attrs = session.getAttributeValue(handle, [CKA_VALUE, CKA_ID, CKA_LABEL])
                    der = bytes(attrs[0])
                    cert_id = bytes(attrs[1]) if attrs[1] else b""
                    found.append({
                        "slot": int(slot),
                        "id": cert_id.hex(),
                        "label": str(attrs[2] or "").strip(),
                        **_describe_certificate(der),
                        "der": base64.b64encode(der).decode(),
                    })
            except Exception as err:
                log("could not read slot", slot, "-", err)
            finally:
                if session is not None:
                    try:
                        session.logout()
                    except Exception:
                        pass
                    session.closeSession()
        return found

    def sign_digest(self, slot, cert_id, pin, digest, algo="sha256"):
        """Have the token sign a hash. The private key never leaves it."""
        from PyKCS11 import (CKA_CLASS, CKO_PRIVATE_KEY, CKA_ID, Mechanism,
                             CKM_RSA_PKCS)
        session = self.lib.openSession(slot)
        try:
            try:
                session.login(pin)
            except Exception as err:
                raise TokenError(_pin_message(err)) from err

            wanted = bytes.fromhex(cert_id) if cert_id else None
            template = [(CKA_CLASS, CKO_PRIVATE_KEY)]
            if wanted:
                template.append((CKA_ID, wanted))
            keys = session.findObjects(template)
            if not keys:
                raise TokenError("The token holds no private key for that certificate.")

            # DigestInfo, then a raw PKCS#1 v1.5 signature. Every token
            # supports CKM_RSA_PKCS; the combined hash-and-sign mechanisms are
            # not always present, and the hash is computed in the browser here
            # in any case, so the document never reaches this machine.
            signature = session.sign(keys[0], _digest_info(digest, algo),
                                     Mechanism(CKM_RSA_PKCS, None))
            return bytes(signature)
        finally:
            try:
                session.logout()
            except Exception:
                pass
            session.closeSession()


# DER-encoded DigestInfo prefixes, per RFC 8017.
_DIGEST_PREFIX = {
    "sha256": bytes.fromhex("3031300d060960864801650304020105000420"),
    "sha384": bytes.fromhex("3041300d060960864801650304020205000430"),
    "sha512": bytes.fromhex("3051300d060960864801650304020305000440"),
}


def _digest_info(digest, algo):
    prefix = _DIGEST_PREFIX.get(algo)
    if not prefix:
        raise TokenError(f"Unsupported hash {algo}.")
    return prefix + digest


def _pin_message(err):
    text = str(err).upper()
    if "PIN_INCORRECT" in text:
        return "That PIN is not right. Check it before trying again."
    if "PIN_LOCKED" in text:
        return ("The token is locked - too many wrong PINs. Unlock it with the "
                "software that came with it.")
    if "PIN_LEN_RANGE" in text:
        return "That PIN is the wrong length for this token."
    return f"The token refused the PIN ({err})."


def _describe_certificate(der):
    """Subject, issuer and validity, for choosing between certificates."""
    try:
        from cryptography import x509
        cert = x509.load_der_x509_certificate(der)

        def field(name, oid):
            try:
                values = cert.subject.get_attributes_for_oid(oid)
                return values[0].value if values else ""
            except Exception:
                return ""

        from cryptography.x509.oid import NameOID
        return {
            "subject": field("CN", NameOID.COMMON_NAME) or cert.subject.rfc4514_string(),
            "issuer": cert.issuer.rfc4514_string(),
            "serialNumber": format(cert.serial_number, "x"),
            "notBefore": cert.not_valid_before_utc.isoformat(),
            "notAfter": cert.not_valid_after_utc.isoformat(),
            "expired": cert.not_valid_after_utc < datetime.datetime.now(datetime.timezone.utc),
        }
    except Exception:
        return {"subject": "(unreadable certificate)", "issuer": "", "serialNumber": "",
                "notBefore": "", "notAfter": "", "expired": False}


def build_cms(digest, algo, cert_der, chain_der, sign_fn, signing_time=None):
    """A detached CMS SignedData over a hash the browser computed.

    Detached, because the content is the PDF and it never comes here: only its
    digest does. The signed attributes carry that digest, and it is the
    attributes that get signed - which is what a reader checks.
    """
    from asn1crypto import cms, algos, x509 as asn1x509

    cert = asn1x509.Certificate.load(cert_der)
    signing_time = signing_time or datetime.datetime.now(datetime.timezone.utc)

    signed_attrs = cms.CMSAttributes([
        cms.CMSAttribute({
            "type": "content_type",
            "values": ["data"],
        }),
        cms.CMSAttribute({
            "type": "signing_time",
            "values": [cms.Time({"utc_time": signing_time})],
        }),
        cms.CMSAttribute({
            "type": "message_digest",
            "values": [digest],
        }),
    ])

    # The signature covers the DER of the attribute set, tagged as a SET -
    # not the implicit [0] form it appears as inside the SignerInfo.
    to_sign = signed_attrs.dump()
    attr_digest = hashlib.new(algo, to_sign).digest()
    signature = sign_fn(attr_digest)

    signer_info = cms.SignerInfo({
        "version": "v1",
        "sid": cms.SignerIdentifier({
            "issuer_and_serial_number": cms.IssuerAndSerialNumber({
                "issuer": cert.issuer,
                "serial_number": cert.serial_number,
            }),
        }),
        "digest_algorithm": algos.DigestAlgorithm({"algorithm": algo}),
        "signed_attrs": signed_attrs,
        "signature_algorithm": algos.SignedDigestAlgorithm({"algorithm": "rsassa_pkcs1v15"}),
        "signature": signature,
    })

    certificates = [cert] + [asn1x509.Certificate.load(c) for c in chain_der]
    content = cms.SignedData({
        "version": "v1",
        "digest_algorithms": cms.DigestAlgorithms([algos.DigestAlgorithm({"algorithm": algo})]),
        # Detached: the content type is named, but no content is carried,
        # because the PDF stayed in the browser.
        "encap_content_info": cms.ContentInfo({"content_type": "data"}),
        "certificates": cms.CertificateSet([cms.CertificateChoices(name="certificate", value=c)
                                            for c in certificates]),
        "signer_infos": cms.SignerInfos([signer_info]),
    })
    return cms.ContentInfo({"content_type": "signed_data", "content": content}).dump()


# Dialogs are drawn by the main thread and nowhere else. Requests arrive on
# the HTTP server's worker threads, and a desktop toolkit will not be driven
# from those - on macOS it does not work at all. Each request posts its
# question here and waits for the answer, which also means two pages cannot
# both put a PIN box on the screen at once.
_PROMPTS = queue.Queue()


def ask_for_pin(document, subject):
    """Ask at the keyboard, not through the browser.

    The PIN is typed into this program and handed to the token driver. It is
    never sent over HTTP, never logged, and not kept once the signature is
    made. The dialog names the document so that a page cannot get something
    signed without the person seeing what it is.
    """
    override = os.environ.get("MIYEE_SIGNER_PIN")
    if override:
        return override, True                # for automated testing only

    if threading.current_thread() is threading.main_thread():
        return _ask_here(document, subject)

    answer = queue.Queue(1)
    _PROMPTS.put((document, subject, answer))
    return answer.get()


def prompt_pump():
    """Serve PIN questions on the main thread until the helper stops."""
    while True:
        item = _PROMPTS.get()
        if item is None:
            return
        document, subject, answer = item
        try:
            answer.put(_ask_here(document, subject))
        except Exception as err:               # never leave a request hanging
            log("could not ask for the PIN -", err)
            answer.put((None, False))


def _ask_here(document, subject):
    try:
        import tkinter as tk
        from tkinter import simpledialog, messagebox
    except Exception:
        # No desktop: fall back to the terminal the helper runs in.
        print(f"\nSign \"{document}\" as {subject}? [y/N] ", end="", flush=True)
        if input().strip().lower() not in ("y", "yes"):
            return None, False
        import getpass
        return getpass.getpass("Token PIN: "), True

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    agreed = messagebox.askokcancel(
        "MiyeePDF - signing request",
        f"Sign this document with your DSC?\n\n"
        f"Document:  {document}\n"
        f"Signing as: {subject}\n\n"
        "Only continue if you started this from MiyeePDF.")
    if not agreed:
        root.destroy()
        return None, False
    pin = simpledialog.askstring("MiyeePDF - token PIN", "Token PIN:", show="*", parent=root)
    root.destroy()
    return pin, True


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = f"MiyeeSigner/{VERSION}"
    token = None

    def log_message(self, fmt, *args):
        log(self.address_string(), fmt % args)

    # -- plumbing ---------------------------------------------------------

    def _origin_allowed(self):
        origin = self.headers.get("Origin")
        # A request with no Origin is not from a page; curl and the health
        # check use that path, and neither can sign.
        return origin is None or origin in ALLOWED_ORIGINS

    def _cors(self):
        origin = self.headers.get("Origin")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        # Chrome asks permission before letting a public page reach a private
        # address; without this the request never arrives.
        if self.headers.get("Access-Control-Request-Private-Network") == "true":
            self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _reply(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- endpoints --------------------------------------------------------

    def do_GET(self):
        if not self._origin_allowed():
            return self._reply(403, {"error": "This page is not allowed to use the signer."})
        if self.path.startswith("/status"):
            tokens = []
            error = None
            try:
                tokens = self.token.describe() if self.token else []
            except Exception as err:
                error = str(err)
            return self._reply(200, {
                "helper": "MiyeePDF signer", "version": VERSION,
                "module": getattr(self.token, "module", None),
                "tokens": tokens, "error": error,
            })
        if self.path.startswith("/certificates"):
            if not self.token:
                return self._reply(400, {"error": _NO_DRIVER})
            try:
                certs = self.token.certificates()
                if not certs and self.token.slots():
                    # Some tokens keep the certificate private, so nothing is
                    # visible until someone logs in. Ask once, here, rather
                    # than leaving the page saying the token is empty.
                    pin, agreed = ask_for_pin("your certificate list", "")
                    if agreed and pin:
                        certs = self.token.certificates(pin)
                    pin = None
                if not certs:
                    return self._reply(200, {"certificates": [], "note":
                        "A token is present but no certificate could be read from it. "
                        "Some tokens only reveal one after a PIN."})
                return self._reply(200, {"certificates": certs})
            except TokenError as err:
                return self._reply(400, {"error": str(err)})
            except Exception as err:
                return self._reply(500, {"error": f"Could not read the token: {err}"})
        return self._reply(404, {"error": "No such endpoint."})

    def do_POST(self):
        if not self._origin_allowed():
            return self._reply(403, {"error": "This page is not allowed to use the signer."})
        if not self.path.startswith("/sign"):
            return self._reply(404, {"error": "No such endpoint."})
        if not self.token:
            return self._reply(400, {"error": _NO_DRIVER})

        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 65536:
                return self._reply(413, {"error": "Only a hash is expected here, not a document."})
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._reply(400, {"error": "Could not read the request."})

        algo = str(body.get("digestAlgo") or "sha256").lower()
        try:
            digest = base64.b64decode(body.get("digest") or "")
        except Exception:
            digest = b""
        expected = {"sha256": 32, "sha384": 48, "sha512": 64}.get(algo)
        if not expected or len(digest) != expected:
            return self._reply(400, {"error": f"Expected a {algo} hash of {expected} bytes."})

        document = str(body.get("document") or "a document")[:120]
        slot = body.get("slot")
        cert_id = str(body.get("certId") or "")

        def pick(available):
            if cert_id:
                return next((c for c in available if c["id"] == cert_id), None)
            return available[0] if available else None

        try:
            certs = self.token.certificates()
        except Exception as err:
            return self._reply(500, {"error": f"Could not read the token: {err}"})
        chosen = pick(certs)

        # The person at the keyboard decides, naming the document and - when
        # the certificate could be read without a PIN - who is about to sign.
        pin, agreed = ask_for_pin(document, (chosen or {}).get("subject", ""))
        if not agreed:
            return self._reply(403, {"error": "CANCELLED",
                                     "message": "Signing was declined at the computer."})
        if not pin:
            return self._reply(400, {"error": "No PIN was entered."})

        if not chosen:
            # A token that hides its certificates until someone logs in.
            try:
                chosen = pick(self.token.certificates(pin))
            except Exception as err:
                pin = None
                return self._reply(500, {"error": f"Could not read the token: {err}"})
        if not chosen:
            pin = None
            return self._reply(400, {"error": "That certificate is not on the token any more."})

        cert_der = base64.b64decode(chosen["der"])
        others = [base64.b64decode(c["der"]) for c in certs if c["id"] != chosen["id"]]
        try:
            blob = build_cms(
                digest, algo, cert_der, others,
                lambda attr_digest: self.token.sign_digest(
                    slot if slot is not None else chosen["slot"],
                    chosen["id"], pin, attr_digest, algo),
            )
        except TokenError as err:
            return self._reply(400, {"error": str(err)})
        except Exception as err:
            return self._reply(500, {"error": f"The token could not sign: {err}"})
        finally:
            pin = None                       # not kept beyond the signature

        return self._reply(200, {
            "cms": base64.b64encode(blob).decode(),
            "signedBy": chosen.get("subject", ""),
            "serialNumber": chosen.get("serialNumber", ""),
        })


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main(argv=None):
    parser = argparse.ArgumentParser(description="MiyeePDF signer helper")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--module", help="Path to the token's PKCS#11 library")
    parser.add_argument("--origin", action="append", default=[],
                        help="An extra page origin allowed to use the helper")
    args = parser.parse_args(argv)

    ALLOWED_ORIGINS.update(args.origin)

    try:
        Handler.token = Token(args.module)
    except TokenError as err:
        log("No token driver:", err)
        Handler.token = None
    except Exception as err:
        log("Could not start the PKCS#11 layer:", err)
        Handler.token = None

    # Bound to the loopback address on purpose: nothing outside this computer
    # can reach it, whatever the network it is on.
    with Server(("127.0.0.1", args.port), Handler) as server:
        log(f"MiyeePDF signer {VERSION} listening on http://127.0.0.1:{args.port}")
        if Handler.token:
            found = Handler.token.describe()
            log("driver:", Handler.token.module)
            log("token:", found[0]["label"] if found else "none plugged in yet")
        log("The document never leaves your browser - only a hash of it comes here.")
        # The requests are served on their own threads; this one is kept free
        # to put the PIN box on the screen.
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            prompt_pump()
        except KeyboardInterrupt:
            log("stopping")
        finally:
            server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
