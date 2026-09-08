"""PDF engine for the Advanced PDF Suite, powered by PyMuPDF.

Runs inside Pyodide in the browser. Every operation works on the real PDF
content stream, so text stays vector text: redaction removes glyphs instead of
painting over them, encryption re-saves the original document rather than
re-photographing it, and compression touches images without flattening pages.

The JavaScript layer keeps documents by id and passes geometry as fractions of
the page (0..1) so it never has to care about PDF points or the flipped y-axis.
"""

import base64
import csv
import difflib
import hashlib
import io
import json
import re

import pymupdf

# doc_id -> pymupdf.Document
_DOCS = {}
# doc_id -> whether the file was password-protected when it was opened.
# Recorded once at open time because reading Document.needs_pass on an
# authenticated document de-authenticates it.
_WAS_ENCRYPTED = {}

# doc_id -> {"undo": [bytes], "redo": [bytes]}
#
# Editing a PDF is destructive: replacing text deletes the original glyphs, so
# without this a mistyped edit could only be escaped by reloading the file and
# losing every other change. Whole-document snapshots are the honest way to
# store that -- an edit can touch fonts, the xref table and several objects at
# once, so a diff of "what changed" would be its own error-prone machine.
#
# Snapshots are capped by count *and* by total bytes, because this runs in the
# browser's memory: a 20 MB scan would otherwise put 240 MB of history beside
# it and take the tab down.
# doc_id -> set of page numbers carrying a text layer this engine added by OCR.
# Exact knowledge beats the heuristic below for the pages we read ourselves.
_OCR_PAGES = {}

# (doc_id, page) -> list of rects the reader was unsure about, as fractions of
# the page. Kept out of the document itself: this is a hint for the person
# editing, not something that belongs in the file they send on.
_OCR_DOUBTS = {}

_HISTORY = {}
_HISTORY_STEPS = 12
_HISTORY_BUDGET = 64 * 1024 * 1024

# Base-14 fonts we can always embed. Real documents reference fonts we cannot
# re-embed from the browser, so editing falls back to the closest of these.
# Each family lists (regular, bold, italic, bold-italic) - PyMuPDF uses fixed
# four-letter names, so these cannot be built by concatenating a suffix.
_FONT_FAMILIES = {
    "helv": ("helv", "hebo", "heit", "hebi"),
    "tiro": ("tiro", "tibo", "tiit", "tibi"),
    "cour": ("cour", "cobo", "coit", "cobi"),
}

_FAMILY_ALIASES = {
    "helvetica": "helv", "arial": "helv", "verdana": "helv", "calibri": "helv",
    "segoeui": "helv", "roboto": "helv", "sans": "helv",
    "times": "tiro", "timesnewroman": "tiro", "georgia": "tiro",
    "garamond": "tiro", "cambria": "tiro", "serif": "tiro", "book": "tiro",
    "courier": "cour", "couriernew": "cour", "consolas": "cour",
    "mono": "cour", "menlo": "cour",
}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _doc(doc_id):
    if doc_id not in _DOCS:
        raise KeyError(f"document '{doc_id}' is not open")
    return _DOCS[doc_id]


def _rect_from_fracs(page, x0, y0, x1, y1):
    """Convert fractional (0..1) top-left coords into a PDF rect."""
    w, h = page.rect.width, page.rect.height
    return pymupdf.Rect(min(x0, x1) * w, min(y0, y1) * h,
                        max(x0, x1) * w, max(y0, y1) * h)


def _hex_to_rgb(value):
    value = (value or "#000000").lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    return tuple(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _int_to_rgb(value):
    """PyMuPDF reports span colours as a packed sRGB int."""
    return (((value >> 16) & 255) / 255, ((value >> 8) & 255) / 255, (value & 255) / 255)


def _pick_font(font_name, flags=0):
    """Map an arbitrary PDF font name onto an embeddable base-14 font."""
    raw = (font_name or "").split("+")[-1]
    name = raw.lower().replace("-", "").replace(" ", "").replace("_", "")
    if "symbol" in name:
        return "symb"
    if "dingbat" in name or "zapf" in name:
        return "zadb"

    bold = "bold" in name or "black" in name or "heavy" in name or bool(flags & 2 ** 4)
    italic = "italic" in name or "oblique" in name or bool(flags & 2 ** 1)

    family = "helv"
    if flags & 2 ** 0:  # fixed-pitch flag
        family = "cour"
    elif flags & 2 ** 1 and "serif" in name:
        family = "tiro"
    for key, value in _FAMILY_ALIASES.items():
        if key in name:
            family = value
            break

    regular, bold_f, italic_f, bolditalic_f = _FONT_FAMILIES[family]
    if bold and italic:
        return bolditalic_f
    if bold:
        return bold_f
    if italic:
        return italic_f
    return regular


def _fit_size(text, font, size, width, measurer=None):
    """Shrink a font size until the text fits the width it has to replace."""
    if width <= 0 or not text:
        return size
    fnt = measurer or pymupdf.Font(font)
    for _ in range(40):
        if fnt.text_length(text, size) <= width or size <= 4:
            break
        size -= 0.5
    return size


# Suffixes that say nothing about the face itself. A PDF's BaseFont entry and
# the name text extraction reports disagree about these constantly - the same
# font turns up as "IPAGothic Regular" in one and "IPAGothic" in the other, or
# as "TimesNewRomanPSMT" against "TimesNewRoman".
_NEUTRAL_FONT_SUFFIXES = ("regular", "roman", "normal", "book", "std", "pro", "mt", "ps")


def _norm_font(value):
    """A font name reduced to the part that identifies the face."""
    name = (value or "").split("+")[-1].lower()
    for ch in "-_ .,":
        name = name.replace(ch, "")
    trimmed = True
    while trimmed:
        trimmed = False
        for suffix in _NEUTRAL_FONT_SUFFIXES:
            if len(name) > len(suffix) and name.endswith(suffix):
                name, trimmed = name[: -len(suffix)], True
    return name


def _same_font(a, b):
    """Whether two font names refer to the same face.

    Weight and slant words are deliberately *not* stripped, so a regular span
    never gets matched to the bold cut sitting next to it on the page.
    """
    return _norm_font(a) == _norm_font(b)


def _grab_embedded_font(doc, page, font_name, text):
    """Pull the document's *own* font file out of the PDF, if it can be reused.

    Real documents use fonts that do not map onto the base-14 set - a Roboto
    invoice rewritten in Helvetica looks obviously edited. When the font is
    embedded we can extract the actual file and write with it.

    Two things stop this from always working. The font may be referenced rather
    than embedded (nothing to extract), and embedded fonts are usually *subset*
    to the glyphs the document already uses, so newly typed characters may be
    missing. Both are checked here; the caller falls back to base-14.

    Returns (buffer, measuring_font) or (None, None). Deliberately returns the
    buffer rather than registering the font, because this must run *before*
    redaction while the page still references it, and the font has to be
    re-registered afterwards.
    """
    needed = {c for c in (text or "") if not c.isspace()}
    for info in page.get_fonts(full=True):
        xref, basefont = info[0], info[3]
        if not _same_font(basefont, font_name):
            continue
        try:
            _name, _ext, _ftype, buf = doc.extract_font(xref)
        except Exception:
            continue
        if not buf:
            continue  # referenced, not embedded - nothing to reuse
        try:
            fnt = pymupdf.Font(fontbuffer=buf)
            if any(not fnt.has_glyph(ord(c)) for c in needed):
                return None, None  # subset lacks a character being typed
            return buf, fnt
        except Exception:
            continue
    return None, None


def _register_font(page, buf, font_name, flags):
    """Attach the reused font to the page, or fall back to a base-14 match.

    Returns (fontname, measuring_font, matched_original).
    """
    if buf:
        try:
            tag = "MPDFembed"
            page.insert_font(fontname=tag, fontbuffer=buf)
            return tag, pymupdf.Font(fontbuffer=buf), True
        except Exception:
            pass
    fallback = _pick_font(font_name, int(flags or 0))
    return fallback, pymupdf.Font(fallback), False


def _page_indices(doc, spec):
    """Parse '1-3, 5' into 0-based indices; empty/None means every page."""
    if not spec or not str(spec).strip():
        return list(range(doc.page_count))
    found = []
    for chunk in str(spec).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            a, _, b = chunk.partition("-")
            try:
                start, end = int(a), int(b)
            except ValueError:
                continue
            if start > end:
                start, end = end, start
            found.extend(range(start - 1, end))
        elif chunk.isdigit():
            found.append(int(chunk) - 1)
    return sorted({i for i in found if 0 <= i < doc.page_count})


# --------------------------------------------------------------------------
# document lifecycle
# --------------------------------------------------------------------------

def open_doc(doc_id, data, password=""):
    """Open a PDF from raw bytes. Returns basic metadata for the UI."""
    close_doc(doc_id)
    doc = pymupdf.open(stream=bytes(data), filetype="pdf")
    # Read needs_pass exactly once, here, before authenticating. Touching it
    # again afterwards silently drops the authentication: every later page read
    # comes back empty and the saved file is corrupt. Remember the answer
    # instead, and never ask the document a second time.
    was_encrypted = bool(doc.needs_pass)
    if was_encrypted:
        if not password or not doc.authenticate(password):
            doc.close()
            raise ValueError("PASSWORD_REQUIRED")
    _DOCS[doc_id] = doc
    _WAS_ENCRYPTED[doc_id] = was_encrypted
    return json.dumps(doc_info(doc_id))


def close_doc(doc_id):
    doc = _DOCS.pop(doc_id, None)
    _WAS_ENCRYPTED.pop(doc_id, None)
    _HISTORY.pop(doc_id, None)
    _OCR_PAGES.pop(doc_id, None)
    for key in [k for k in _OCR_DOUBTS if k[0] == doc_id]:
        _OCR_DOUBTS.pop(key, None)
    if doc is not None:
        doc.close()
    return True


# --------------------------------------------------------------------------
# undo / redo
# --------------------------------------------------------------------------

def _trim_history(entry):
    """Keep history inside both caps, dropping the oldest states first."""
    while len(entry["undo"]) > _HISTORY_STEPS:
        entry["undo"].pop(0)
    while (len(entry["undo"]) > 1
           and sum(len(b) for b in entry["undo"]) > _HISTORY_BUDGET):
        entry["undo"].pop(0)


def _serialise(doc_id):
    """A restorable copy of the current document.

    Deliberately not garbage-collected or cleaned: this is a throwaway
    snapshot, and the tidying passes are the slow part of saving.
    """
    return _doc(doc_id).tobytes(garbage=0, deflate=True)


def _restore(doc_id, data):
    """Swap the working document for a snapshot, keeping the id valid."""
    old = _DOCS.get(doc_id)
    _DOCS[doc_id] = pymupdf.open(stream=bytes(data), filetype="pdf")
    if old is not None:
        old.close()


def snapshot(doc_id):
    """Record the current state so the next change can be undone.

    Called before a mutation, not after, so the stack holds the states to go
    back *to*. Any new change clears the redo stack, as in every editor.
    """
    if doc_id not in _DOCS:
        return False
    entry = _HISTORY.setdefault(doc_id, {"undo": [], "redo": []})
    try:
        entry["undo"].append(_serialise(doc_id))
    except Exception:
        return False       # never let bookkeeping break the actual edit
    entry["redo"].clear()
    _trim_history(entry)
    return True


def history_state(doc_id):
    """What the undo and redo buttons should show."""
    entry = _HISTORY.get(doc_id) or {"undo": [], "redo": []}
    return json.dumps({"undo": len(entry["undo"]), "redo": len(entry["redo"])})


def undo(doc_id):
    """Step back one change. Returns the new history state."""
    entry = _HISTORY.get(doc_id)
    if not entry or not entry["undo"]:
        return json.dumps({"ok": False, "undo": 0,
                           "redo": len(entry["redo"]) if entry else 0})
    current = _serialise(doc_id)
    _restore(doc_id, entry["undo"].pop())
    entry["redo"].append(current)
    return json.dumps({"ok": True, "undo": len(entry["undo"]),
                       "redo": len(entry["redo"])})


def redo(doc_id):
    """Step forward again after an undo."""
    entry = _HISTORY.get(doc_id)
    if not entry or not entry["redo"]:
        return json.dumps({"ok": False, "redo": 0,
                           "undo": len(entry["undo"]) if entry else 0})
    current = _serialise(doc_id)
    _restore(doc_id, entry["redo"].pop())
    entry["undo"].append(current)
    _trim_history(entry)
    return json.dumps({"ok": True, "undo": len(entry["undo"]),
                       "redo": len(entry["redo"])})


def doc_info(doc_id):
    doc = _doc(doc_id)
    meta = doc.metadata or {}
    return {
        "pages": doc.page_count,
        "encrypted": _WAS_ENCRYPTED.get(doc_id, False),
        "title": meta.get("title") or "",
        "author": meta.get("author") or "",
        "sizes": [
            {"width": p.rect.width, "height": p.rect.height, "rotation": p.rotation}
            for p in doc
        ],
    }


def page_count(doc_id):
    return _doc(doc_id).page_count


def render_page(doc_id, pno, dpi=100):
    """Render one page to PNG bytes for display."""
    page = _doc(doc_id)[int(pno)]
    return page.get_pixmap(dpi=int(dpi)).tobytes("png")


def render_thumb(doc_id, pno, width=132):
    """Render one page small, for the thumbnail rail.

    Sized by pixel width rather than dpi so every thumbnail comes out the same
    width whatever the paper size, which is what lets the rail line up. A4 and
    Letter and a wide plan drawing all land on the same column.
    """
    page = _doc(doc_id)[int(pno)]
    scale = float(width) / max(page.rect.width, 1.0)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
    return pix.tobytes("png")


def save(doc_id, garbage=3, deflate=True):
    """Serialise the working document."""
    return _doc(doc_id).tobytes(garbage=int(garbage), deflate=bool(deflate), clean=True)


# --------------------------------------------------------------------------
# text editing (real content-stream replacement)
# --------------------------------------------------------------------------

def get_spans(doc_id, pno):
    """Every text span on a page with the metrics needed to re-typeset it."""
    page = _doc(doc_id)[int(pno)]
    pw, ph = page.rect.width, page.rect.height
    spans = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                text = span["text"]
                if not text.strip():
                    continue
                x0, y0, x1, y1 = span["bbox"]
                spans.append({
                    "text": text,
                    "font": span["font"],
                    "size": round(span["size"], 2),
                    "colorInt": span["color"],
                    "flags": span["flags"],
                    "bbox": [x0, y0, x1, y1],
                    # fractions so the UI can place an overlay without knowing PDF units
                    "xFrac": x0 / pw, "yFrac": y0 / ph,
                    "wFrac": (x1 - x0) / pw, "hFrac": (y1 - y0) / ph,
                })
    return json.dumps(spans)


def get_web_font(doc_id, pno, font_name):
    """The page's own embedded font, in a form a browser can render.

    Used by the in-place editor so what you type on screen looks like what
    the PDF already contains. TrueType and OpenType go straight to the
    browser; anything else (bare CFF, Type1) comes back empty and the UI
    falls back to a lookalike stack.
    """
    doc = _doc(doc_id)
    page = doc[int(pno)]
    for info in page.get_fonts(full=True):
        xref, basefont = info[0], info[3]
        if not _same_font(basefont, font_name):
            continue
        try:
            _name, ext, _ftype, buf = doc.extract_font(xref)
        except Exception:
            continue
        if buf and ext in ("ttf", "otf"):
            return buf
    return b""


def edit_text(doc_id, pno, bbox, new_text, font_name="", size=0, color_int=0,
              flags=0, cover=False):
    """Replace the text in one span, keeping the original look.

    The old glyphs are genuinely removed from the content stream (redaction),
    then the replacement is drawn with matched font, size and colour.

    `cover` is for scanned pages. There the words you see are pixels in an
    image, and the text objects are only the invisible layer OCR added on top:
    removing them changes nothing visible, so the old wording would still show
    through under the new. With cover set, the paper colour is sampled from
    the scan itself and painted over the region first.
    """
    doc = _doc(doc_id)
    page = doc[int(pno)]
    x0, y0, x1, y1 = [float(v) for v in bbox]
    rect = pymupdf.Rect(x0, y0, x1, y1)

    # Grab the embedded font while the page still references it - redaction can
    # drop the resource.
    buf, _ = _grab_embedded_font(doc, page, font_name, new_text)

    # Erase the original glyphs without disturbing anything else on the page.
    page.add_redact_annot(rect)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE)

    if cover:
        # Pad a little: glyphs are anti-aliased, and their bounding box clips
        # descenders and the faint edge pixels that would otherwise survive as
        # a grey ghost of the old word.
        pad = max(rect.height * 0.12, 0.6)
        patch = pymupdf.Rect(rect.x0 - pad, rect.y0 - pad, rect.x1 + pad, rect.y1 + pad)
        page.draw_rect(patch, color=None, fill=_sample_background(page, patch),
                       width=0, overlay=True)

    if not new_text:
        return json.dumps({"ok": True, "font": "", "exact": False, "covered": bool(cover)})

    font, measurer, exact = _register_font(page, buf, font_name, flags)
    size = float(size) or (rect.height * 0.8)
    size = _fit_size(new_text, font, size, rect.width * 1.15, measurer)
    page.insert_text(
        (rect.x0, rect.y1 - size * 0.22),
        new_text,
        fontsize=size,
        fontname=font,
        color=_int_to_rgb(int(color_int)),
    )
    return json.dumps({"ok": True, "font": font_name if exact else _pick_font(font_name, int(flags or 0)),
                       "exact": exact, "size": round(size, 1), "covered": bool(cover)})


def insert_text(doc_id, pno, x_frac, y_frac, text, size=12, color="#000000",
                bold=False, italic=False, width_frac=0.0):
    """Add new text at a point on the page.

    Editing existing text was the only way to change a document, so anything
    the original did not already say could not be added at all. The text is
    written as real text, not an annotation, so it is selectable and
    searchable like the rest of the page.

    A width hint wraps the text into a box; without one it is a single line.
    """
    doc = _doc(doc_id)
    page = doc[int(pno)]
    text = str(text or "")
    if not text.strip():
        return json.dumps({"ok": False, "reason": "Nothing to add."})

    flags = (16 if bold else 0) | (2 if italic else 0)
    font = _pick_font("", flags)
    size = max(float(size or 12), 1.0)
    rgb = _hex_to_rgb(color)
    pw, ph = page.rect.width, page.rect.height
    x = max(0.0, min(float(x_frac), 1.0)) * pw
    y = max(0.0, min(float(y_frac), 1.0)) * ph

    wrap = float(width_frac or 0) * pw
    if wrap > size or "\n" in text:
        # Wrapped: give it room below and let PyMuPDF reflow inside the box.
        box = pymupdf.Rect(x, y, min(x + (wrap or pw - x), pw), ph)
        leftover = page.insert_textbox(box, text, fontsize=size, fontname=font,
                                       color=rgb, align=0)
        if leftover < 0:      # did not fit: shrink until it does
            for trial in (size * 0.9, size * 0.8, size * 0.7, size * 0.6):
                if page.insert_textbox(box, text, fontsize=trial, fontname=font,
                                       color=rgb, align=0) >= 0:
                    size = trial
                    break
    else:
        # insert_text places the baseline; the click should feel like the top
        # of the text, so drop by roughly the ascender.
        page.insert_text((x, y + size * 0.82), text, fontsize=size,
                         fontname=font, color=rgb)
    return json.dumps({"ok": True, "size": round(size, 1)})


def search_text(doc_id, needle, context=48):
    """Find a phrase across the document.

    Each hit carries the line it sits on as well as its rectangle. A list of
    twenty buttons all reading "Page 4" tells you nothing about which one you
    want; the surrounding words do.
    """
    doc = _doc(doc_id)
    term = str(needle)
    hits = []
    for pno, page in enumerate(doc):
        pw, ph = page.rect.width, page.rect.height
        for r in page.search_for(term):
            # The whole line the hit sits on, taken from the page rather than
            # reconstructed, so it reads as it does on paper.
            band = pymupdf.Rect(0, r.y0 - 1, pw, r.y1 + 1)
            line = " ".join(page.get_textbox(band).split())
            at = line.lower().find(term.lower())
            if at < 0:
                before, hit, after = "", term, ""
            else:
                start = max(at - int(context), 0)
                before = ("…" if start else "") + line[start:at]
                hit = line[at:at + len(term)]
                end = at + len(term) + int(context)
                after = line[at + len(term):end] + ("…" if end < len(line) else "")
            hits.append({
                "page": pno,
                "xFrac": r.x0 / pw, "yFrac": r.y0 / ph,
                "wFrac": r.width / pw, "hFrac": r.height / ph,
                "before": before, "hit": hit, "after": after,
            })
    return json.dumps(hits)


# --------------------------------------------------------------------------
# redaction
# --------------------------------------------------------------------------

# Redaction over an image has to cut the pixels out of the image data, not
# paint a rectangle on top of it. IMAGE_NONE leaves the picture whole and the
# covered area is still there for anyone who extracts it - which is exactly
# the "black box you can recover" this tool exists not to be, and on a scan
# every word on the page is inside an image. PIXELS blanks only the covered
# part, so the rest of the scan survives; REMOVE throws the whole image away
# and is offered separately for when that is what you want.
_REDACT_IMAGES = pymupdf.PDF_REDACT_IMAGE_PIXELS


def redact(doc_id, marks_json, remove_images=False, fill="#000000"):
    """Permanently remove content under each mark.

    Unlike drawing a black box, the underlying text and image data are struck
    from the file, so the result cannot be recovered by copy/paste. `fill` only
    changes the colour of the box left behind - never what gets removed.
    """
    doc = _doc(doc_id)
    marks = json.loads(marks_json) if isinstance(marks_json, str) else marks_json
    colour = _hex_to_rgb(fill)
    touched = set()
    for mark in marks:
        page = doc[int(mark["page"])]
        rect = _rect_from_fracs(page, float(mark["xFrac"]), float(mark["yFrac"]),
                                float(mark["xFrac"]) + float(mark["wFrac"]),
                                float(mark["yFrac"]) + float(mark["hFrac"]))
        page.add_redact_annot(rect, fill=colour)
        touched.add(int(mark["page"]))

    mode = pymupdf.PDF_REDACT_IMAGE_REMOVE if remove_images else _REDACT_IMAGES
    for pno in touched:
        doc[pno].apply_redactions(images=mode)
    return len(touched)


def redact_search(doc_id, needle, pages_spec="", fill="#000000"):
    """Find every occurrence of a phrase and redact it (Acrobat's find-and-redact)."""
    doc = _doc(doc_id)
    colour = _hex_to_rgb(fill)
    count = 0
    for pno in _page_indices(doc, pages_spec):
        page = doc[pno]
        found = page.search_for(needle)
        for rect in found:
            page.add_redact_annot(rect, fill=colour)
        if found:
            page.apply_redactions(images=_REDACT_IMAGES)
            count += len(found)
    return count


# --------------------------------------------------------------------------
# protection
# --------------------------------------------------------------------------

def encrypt(doc_id, user_pw, owner_pw="", allow_print=True, allow_copy=False,
            allow_modify=False, allow_annotate=False):
    """AES-256 encrypt the document itself - pages are never rasterised."""
    doc = _doc(doc_id)
    perms = 0
    if allow_print:
        perms |= pymupdf.PDF_PERM_PRINT | pymupdf.PDF_PERM_PRINT_HQ
    if allow_copy:
        perms |= pymupdf.PDF_PERM_COPY
    if allow_modify:
        perms |= pymupdf.PDF_PERM_MODIFY
    if allow_annotate:
        perms |= pymupdf.PDF_PERM_ANNOTATE

    return doc.tobytes(
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        owner_pw=owner_pw or user_pw,
        user_pw=user_pw,
        permissions=perms,
        garbage=3,
        deflate=True,
    )


def decrypt(doc_id):
    """Save without encryption (requires the document to be open/authenticated)."""
    return _doc(doc_id).tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE, garbage=3, deflate=True)


# --------------------------------------------------------------------------
# compression
# --------------------------------------------------------------------------

def compress(doc_id, level="medium"):
    """Shrink the file by recompressing images and cleaning the object tree.

    Text and vector art are left untouched, so the result stays searchable.
    """
    doc = _doc(doc_id)
    presets = {
        "low": (0.9, 300),
        "medium": (0.75, 150),
        "high": (0.55, 96),
    }
    quality, max_dpi = presets.get(level, presets["medium"])

    for pno in range(doc.page_count):
        page = doc[pno]
        for info in page.get_images(full=True):
            xref = info[0]
            try:
                base = doc.extract_image(xref)
            except Exception:
                continue
            if doc.xref_get_key(xref, "SMask")[0] != "null":
                continue
            pix = pymupdf.Pixmap(doc, xref)
            if pix.n - pix.alpha >= 4:  # CMYK -> RGB so JPEG is valid
                pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
            if pix.alpha:
                pix = pymupdf.Pixmap(pix, 0)

            # Downsample anything wildly larger than it is displayed.
            rects = page.get_image_rects(xref)
            if rects:
                shown_w = max(r.width for r in rects) or 1
                target_w = min(pix.width, int(shown_w * max_dpi / 72))
                if 0 < target_w < pix.width:
                    scale = target_w / pix.width
                    pix = pymupdf.Pixmap(pix, 0)
                    pix.shrink(max(1, int(1 / max(scale, 1e-6)) // 2) or 1)

            try:
                new_bytes = pix.tobytes("jpeg", jpg_quality=int(quality * 100))
            except Exception:
                continue
            if len(new_bytes) < len(base.get("image", b"")):
                try:
                    # compress=False is the whole point: update_stream deflates
                    # by default, which would leave a Flate-wrapped JPEG under a
                    # dictionary claiming /DCTDecode. Lenient readers cope;
                    # strict ones drop the image, so a scanned page opens blank.
                    doc.update_stream(xref, new_bytes, new=True, compress=False)
                    doc.xref_set_key(xref, "Filter", "/DCTDecode")
                    doc.xref_set_key(xref, "ColorSpace",
                                     "/DeviceGray" if pix.n == 1 else "/DeviceRGB")
                    doc.xref_set_key(xref, "BitsPerComponent", "8")
                    doc.xref_set_key(xref, "Width", str(pix.width))
                    doc.xref_set_key(xref, "Height", str(pix.height))
                    # Whatever described the old stream cannot describe this one.
                    doc.xref_set_key(xref, "DecodeParms", "null")
                    doc.xref_set_key(xref, "Decode", "null")
                except Exception:
                    pass

    return doc.tobytes(garbage=4, deflate=True, clean=True, deflate_images=True,
                       deflate_fonts=True)


# --------------------------------------------------------------------------
# page organisation
# --------------------------------------------------------------------------

_CONTENTS_MARGIN = 56
_CONTENTS_LEADING = 22


def _contents_pages(out, entries, title, lead):
    """A contents page at the front, listing each document and where it starts.

    Written before the pages it points at exist in their final positions, so
    the number of contents pages has to be settled first - the page numbers
    printed on it depend on how many pages it occupies itself.
    """
    shape = out[0].rect if out.page_count else pymupdf.paper_rect("a4")
    width, height = shape.width, shape.height
    usable = height - 2 * _CONTENTS_MARGIN - 40          # 40 for the heading
    per_page = max(1, int(usable // _CONTENTS_LEADING))

    # Every contents page has to exist before a single link is written, because
    # inserting one shifts the pages after it - and a link binds to the page
    # that is at that position when it is made, not to the number. Writing as
    # we went made every link on the first page point one page too far.
    for index in range(lead):
        out.new_page(pno=index, width=width, height=height)

    for index in range(lead):
        page = out[index]
        page.insert_text((_CONTENTS_MARGIN, _CONTENTS_MARGIN + 12),
                         title if index == 0 else f"{title} (continued)",
                         fontname="hebo", fontsize=16)
        y = _CONTENTS_MARGIN + 46
        for label, target in entries[index * per_page:(index + 1) * per_page]:
            # The label can be long; the page number must stay readable, so the
            # label is trimmed rather than allowed to run into it.
            room = width - 2 * _CONTENTS_MARGIN - 46
            text = label
            while pymupdf.get_text_length(text, fontname="helv", fontsize=11) > room and len(text) > 4:
                text = text[:-2]
            if text != label:
                # Three dots, not an ellipsis character: the built-in
                # Helvetica has no glyph for it and draws a stray dot instead.
                text = text[:-3] + "..."
            page.insert_text((_CONTENTS_MARGIN, y), text, fontname="helv", fontsize=11)
            number = str(target + 1)
            page.insert_text((width - _CONTENTS_MARGIN -
                              pymupdf.get_text_length(number, fontname="helv", fontsize=11), y),
                             number, fontname="helv", fontsize=11)
            # The whole row is a link, so the contents page is usable on screen
            # as well as on paper.
            page.insert_link({
                "kind": pymupdf.LINK_GOTO, "page": target,
                "from": pymupdf.Rect(_CONTENTS_MARGIN, y - 12, width - _CONTENTS_MARGIN, y + 4),
                "to": pymupdf.Point(0, 0),
            })
            y += _CONTENTS_LEADING
    return per_page


def merge(doc_ids_json, labels_json="", bookmarks=False, contents=False, title="Contents"):
    """Concatenate several open documents, in the order given.

    A bundle of thirty annexures is not much use as thirty concatenated pages
    with no way in, so the parts can be kept findable: a bookmark per source
    document - with that document's own bookmarks nested underneath it, rather
    than thrown away - and a contents page at the front whose rows are links.
    """
    ids = json.loads(doc_ids_json) if isinstance(doc_ids_json, str) else list(doc_ids_json)
    labels = json.loads(labels_json) if labels_json else []
    out = pymupdf.open()

    starts = []
    inner = []
    for doc_id in ids:
        source = _doc(doc_id)
        starts.append(out.page_count)
        try:
            inner.append(source.get_toc(simple=True) or [])
        except Exception:
            inner.append([])
        out.insert_pdf(source)

    def label_for(index):
        name = labels[index] if index < len(labels) else ""
        return str(name).strip() or f"Document {index + 1}"

    # Only worth doing for a real bundle: one document needs neither.
    wanted = len(ids) > 1 and out.page_count
    lead = 0
    if wanted and contents:
        shape = out[0].rect
        usable = shape.height - 2 * _CONTENTS_MARGIN - 40
        per_page = max(1, int(usable // _CONTENTS_LEADING))
        lead = max(1, -(-len(ids) // per_page))
        _contents_pages(out, [(label_for(i), starts[i] + lead) for i in range(len(ids))],
                        title, lead)

    if wanted and bookmarks:
        toc = []
        if lead:
            toc.append([1, title, 1])
        for index, start in enumerate(starts):
            toc.append([1, label_for(index), start + lead + 1])
            for entry in inner[index]:
                level, name, pno = entry[0], entry[1], entry[2]
                if pno > 0:
                    toc.append([level + 1, name, start + lead + pno])
        out.set_toc(toc)

    data = out.tobytes(garbage=3, deflate=True)
    out.close()
    return data


def split(doc_id, mode="individual", spec="", every=1):
    """Split into parts. Returns [{name, b64}] so JS can zip them."""
    doc = _doc(doc_id)
    parts = []

    def emit(name, start, end):
        piece = pymupdf.open()
        piece.insert_pdf(doc, from_page=start, to_page=end)
        parts.append({
            "name": name,
            "b64": base64.b64encode(piece.tobytes(garbage=3, deflate=True)).decode(),
        })
        piece.close()

    if mode == "ranges":
        for idx, chunk in enumerate([c.strip() for c in str(spec).split(",") if c.strip()], 1):
            idxs = _page_indices(doc, chunk)
            if idxs:
                emit(f"part-{idx}.pdf", idxs[0], idxs[-1])
    elif mode == "every":
        step = max(1, int(every))
        for idx, start in enumerate(range(0, doc.page_count, step), 1):
            emit(f"part-{idx}.pdf", start, min(start + step - 1, doc.page_count - 1))
    else:
        for pno in range(doc.page_count):
            emit(f"page-{pno + 1}.pdf", pno, pno)

    return json.dumps(parts)


def organize(doc_id, order_json, rotations_json="{}", crops_json="{}"):
    """Reorder/rotate/crop/drop pages. `order_json` lists indices to keep.

    The arguments arrive as JSON strings: values handed straight over the
    JS bridge become JsProxy objects, which lack the dict/list API used here.
    """
    doc = _doc(doc_id)
    order = json.loads(order_json) if isinstance(order_json, str) else list(order_json)
    rotations = json.loads(rotations_json) if isinstance(rotations_json, str) else dict(rotations_json)
    crops = json.loads(crops_json) if isinstance(crops_json, str) else dict(crops_json or {})

    # A full copy, then select the pages to keep. Building the result page by
    # page with insert_pdf loses the outline entirely - a bundle's bookmarks
    # and the links on its contents page vanished the moment it was saved.
    # select() carries both across and renumbers them.
    out = pymupdf.open(stream=doc.tobytes(), filetype="pdf")
    out.select([int(i) for i in order])
    for pos, original in enumerate(order):
        page = out[pos]
        box = crops.get(str(original), crops.get(int(original)))
        if box:
            # The crop box is what a reader shows and what a printer prints.
            # The content outside it is still in the file - trimming a margin
            # is not redaction, and this must not be mistaken for it.
            whole = page.rect
            page.set_cropbox(pymupdf.Rect(
                whole.x0 + float(box["x0"]) * whole.width,
                whole.y0 + float(box["y0"]) * whole.height,
                whole.x0 + float(box["x1"]) * whole.width,
                whole.y0 + float(box["y1"]) * whole.height,
            ))
        rot = rotations.get(str(original), rotations.get(int(original), 0)) or 0
        if rot:
            page.set_rotation((page.rotation + int(rot)) % 360)
    data = out.tobytes(garbage=3, deflate=True)
    out.close()
    return data


# --------------------------------------------------------------------------
# watermarks, stamps, numbering
# --------------------------------------------------------------------------

def watermark_text(doc_id, text, pages_spec="", size=48, color="#c81e1e", opacity=0.3,
                   rotate=45, tiled=False):
    doc = _doc(doc_id)
    rgb = _hex_to_rgb(color)
    font = pymupdf.Font("hebo")
    for pno in _page_indices(doc, pages_spec):
        page = doc[pno]
        w, h = page.rect.width, page.rect.height
        text_w = font.text_length(text, size)

        spots = []
        if tiled:
            step_x, step_y = text_w + 90, size * 3.2
            y = step_y / 2
            while y < h + step_y:
                x = step_x / 2
                while x < w + step_x:
                    spots.append((x, y))
                    x += step_x
                y += step_y
        else:
            spots.append((w / 2, h / 2))

        for cx, cy in spots:
            # Rotate about the text's own centre so tiling stays even.
            morph = ((pymupdf.Point(cx, cy), pymupdf.Matrix(float(rotate)))
                     if rotate else None)
            page.insert_text(
                (cx - text_w / 2, cy + size * 0.35), text,
                fontsize=size, fontname="hebo", color=rgb,
                fill_opacity=float(opacity), morph=morph,
            )
    return True


def watermark_image(doc_id, image_b64, pages_spec="", opacity=0.3, scale=0.5, tiled=False):
    doc = _doc(doc_id)
    raw = base64.b64decode(image_b64.split(",")[-1])
    for pno in _page_indices(doc, pages_spec):
        page = doc[pno]
        w, h = page.rect.width, page.rect.height
        iw = w * float(scale)
        pix = pymupdf.Pixmap(raw)
        ih = iw * (pix.height / pix.width)

        spots = []
        if tiled:
            y = ih / 2
            while y < h + ih:
                x = iw / 2
                while x < w + iw:
                    spots.append((x, y))
                    x += iw + 40
                y += ih + 40
        else:
            spots.append((w / 2, h / 2))

        for cx, cy in spots:
            rect = pymupdf.Rect(cx - iw / 2, cy - ih / 2, cx + iw / 2, cy + ih / 2)
            page.insert_image(rect, stream=raw, overlay=True, alpha=int(float(opacity) * 255))
    return True


def stamp(doc_id, stamps_json, color="#c81e1e"):
    """Place boxed approval stamps at fractional positions."""
    doc = _doc(doc_id)
    items = json.loads(stamps_json) if isinstance(stamps_json, str) else stamps_json
    rgb = _hex_to_rgb(color)
    font = pymupdf.Font("hebo")
    for item in items:
        page = doc[int(item["page"])]
        rect = _rect_from_fracs(page, float(item["xFrac"]), float(item["yFrac"]),
                                float(item["xFrac"]) + float(item["wFrac"]),
                                float(item["yFrac"]) + float(item["hFrac"]))
        page.draw_rect(rect, color=rgb, width=2.5)
        text = item["text"]
        size = _fit_size(text, "hebo", rect.height * 0.55, rect.width * 0.88)
        tw = font.text_length(text, size)
        page.insert_text((rect.x0 + (rect.width - tw) / 2, rect.y0 + rect.height / 2 + size * 0.35),
                         text, fontsize=size, fontname="hebo", color=rgb)
    return True


def page_numbers(doc_id, fmt="Page {n} of {total}", position="bottom-center",
                 start=1, size=10, color="#111111", margin=28):
    doc = _doc(doc_id)
    rgb = _hex_to_rgb(color)
    font = pymupdf.Font("helv")
    total = doc.page_count
    for idx, page in enumerate(doc):
        label = (fmt.replace("{n}", str(start + idx))
                    .replace("{total}", str(total))
                    .replace("{page}", str(start + idx)))
        tw = font.text_length(label, size)
        w, h = page.rect.width, page.rect.height
        x = margin if "left" in position else (w - margin - tw if "right" in position else (w - tw) / 2)
        y = margin if position.startswith("top") else h - margin
        page.insert_text((x, y), label, fontsize=size, fontname="helv", color=rgb)
    return True


def bates(doc_id, prefix="", suffix="", start=1, digits=6, position="bottom-right",
          size=10, color="#111111", margin=28):
    """Bates numbering - sequential legal stamps like BATES000001."""
    doc = _doc(doc_id)
    rgb = _hex_to_rgb(color)
    font = pymupdf.Font("helv")
    for idx, page in enumerate(doc):
        label = f"{prefix}{str(int(start) + idx).zfill(int(digits))}{suffix}"
        tw = font.text_length(label, size)
        w, h = page.rect.width, page.rect.height
        x = margin if "left" in position else (w - margin - tw if "right" in position else (w - tw) / 2)
        y = margin if position.startswith("top") else h - margin
        page.insert_text((x, y), label, fontsize=size, fontname="helv", color=rgb)
    return doc.page_count


def header_footer(doc_id, fields_json, size=9, color="#111111", margin=28, filename=""):
    """Headers/footers with {page}, {total}, {date}, {filename} placeholders."""
    import datetime
    doc = _doc(doc_id)
    fields = json.loads(fields_json) if isinstance(fields_json, str) else fields_json
    rgb = _hex_to_rgb(color)
    font = pymupdf.Font("helv")
    today = datetime.date.today().strftime("%Y-%m-%d")
    total = doc.page_count

    for idx, page in enumerate(doc):
        w, h = page.rect.width, page.rect.height
        for key, raw in fields.items():
            if not raw or not str(raw).strip():
                continue
            label = (str(raw).replace("{page}", str(idx + 1))
                             .replace("{total}", str(total))
                             .replace("{date}", today)
                             .replace("{filename}", filename))
            vertical, _, horizontal = key.partition("-")
            tw = font.text_length(label, size)
            x = margin if horizontal == "left" else (w - margin - tw if horizontal == "right" else (w - tw) / 2)
            y = margin if vertical == "header" else h - margin
            page.insert_text((x, y), label, fontsize=size, fontname="helv", color=rgb)
    return True


# --------------------------------------------------------------------------
# fill & sign
# --------------------------------------------------------------------------

def place_items(doc_id, items_json):
    """Drop signatures, initials, text, dates and check marks onto pages.

    Items carry fractional geometry from the UI; images arrive as data URLs.
    """
    doc = _doc(doc_id)
    items = json.loads(items_json) if isinstance(items_json, str) else items_json

    for item in items:
        page = doc[int(item["page"])]
        rect = _rect_from_fracs(page, float(item["xFrac"]), float(item["yFrac"]),
                                float(item["xFrac"]) + float(item["wFrac"]),
                                float(item["yFrac"]) + float(item["hFrac"]))
        if item.get("dataUrl"):
            raw = base64.b64decode(item["dataUrl"].split(",")[-1])
            page.insert_image(rect, stream=raw, overlay=True)
        else:
            text = item.get("text", "")
            if not text:
                continue
            font = _pick_font(item.get("font", "helv"))
            size = float(item.get("size") or rect.height * 0.7)
            size = _fit_size(text, font, size, rect.width)
            page.insert_text((rect.x0, rect.y1 - size * 0.25), text, fontsize=size,
                             fontname=font, color=_hex_to_rgb(item.get("color", "#000000")))
    return len(items)


def fill_form(doc_id, values_json, flatten=True):
    """Fill AcroForm fields by name, optionally flattening them into the page."""
    doc = _doc(doc_id)
    values = json.loads(values_json) if isinstance(values_json, str) else values_json
    filled = 0
    for page in doc:
        for widget in page.widgets():
            if widget.field_name not in values:
                continue
            value = values[widget.field_name]
            try:
                if widget.field_type == pymupdf.PDF_WIDGET_TYPE_CHECKBOX:
                    widget.field_value = bool(value)
                else:
                    widget.field_value = str(value)
                widget.update()
                filled += 1
            except Exception:
                continue
    if flatten and filled:
        # Re-saving with the form "baked" keeps values visible everywhere.
        doc.bake()
    return filled


def get_form_fields(doc_id):
    """List fillable form fields so the UI can render inputs for them."""
    doc = _doc(doc_id)
    out = []
    types = {
        pymupdf.PDF_WIDGET_TYPE_CHECKBOX: "checkbox",
        pymupdf.PDF_WIDGET_TYPE_TEXT: "text",
        pymupdf.PDF_WIDGET_TYPE_COMBOBOX: "select",
        pymupdf.PDF_WIDGET_TYPE_LISTBOX: "select",
        pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON: "radio",
    }
    for pno, page in enumerate(doc):
        for widget in page.widgets():
            out.append({
                "page": pno,
                "name": widget.field_name,
                "kind": types.get(widget.field_type, "text"),
                "value": widget.field_value if isinstance(widget.field_value, str) else "",
                "options": list(widget.choice_values or []) if widget.choice_values else [],
            })
    return json.dumps(out)


# --------------------------------------------------------------------------
# OCR - build a genuinely searchable PDF
# --------------------------------------------------------------------------

def needs_ocr(doc_id, pno):
    """A page with (almost) no extractable text is presumed to be a scan."""
    return len(_doc(doc_id)[int(pno)].get_text().strip()) < 12


def page_status(doc_id, pno):
    """What the Edit tab needs to know before offering to edit a page.

    A scanned page carries no text objects at all, so there is nothing to
    click and the tab would otherwise sit there looking broken. Reporting
    this lets the editor say what the page actually is and offer OCR.
    """
    page = _doc(doc_id)[int(pno)]
    chars = len(page.get_text().strip())
    images = len(page.get_images(full=True))
    # Text sitting on top of a page-sized image is the invisible layer OCR
    # leaves behind: the words are selectable, but what you *see* is the scan,
    # so editing has to paint over pixels rather than only replace glyphs.
    ocr_layer = int(pno) in _OCR_PAGES.get(doc_id, set())
    if not ocr_layer and images and chars:
        # A file that arrived already OCR'd: text sitting on top of imagery
        # that covers most of the page. Total coverage is the test, not one
        # dominant image -- scanners often tile a page into many strips, and
        # a single-image check misses those completely.
        page_area = abs(page.rect.width * page.rect.height) or 1
        covered = 0.0
        for info in page.get_images(full=True):
            for r in page.get_image_rects(info[0]):
                covered += abs(r.width * r.height)
        ocr_layer = (covered / page_area) > 0.5
    return json.dumps({
        "chars": chars,
        "images": images,
        "scanned": chars < 12 and images > 0,
        "ocrLayer": ocr_layer,
    })


def _sample_background(page, rect):
    """The scan's own background colour beneath a region, as a 0..1 RGB tuple.

    Editing a scanned page means painting over pixels, and a scan is rarely
    pure white -- it is off-white, grey or faintly coloured, so a white patch
    would read as an obvious sticker. The most common colour inside the strip
    is the paper behind the text, since background outnumbers ink.
    """
    try:
        clip = pymupdf.Rect(rect) & page.rect
        if clip.is_empty:
            return (1, 1, 1)
        pix = page.get_pixmap(dpi=48, clip=clip, colorspace=pymupdf.csRGB, alpha=False)
        if not pix.width or not pix.height:
            return (1, 1, 1)
        data = pix.samples
        # Two passes. Coarse buckets first, because scan noise means exact
        # colours almost never repeat and a raw histogram finds no majority.
        counts = {}
        for i in range(0, len(data) - 2, 3):
            key = (data[i] >> 4, data[i + 1] >> 4, data[i + 2] >> 4)
            counts[key] = counts.get(key, 0) + 1
        if not counts:
            return (1, 1, 1)
        winner = max(counts, key=counts.get)
        # Then average the real values inside the winning bucket. Using the
        # bucket's midpoint instead leaves the patch a few levels off the paper
        # around it, which reads as a pale rectangle stuck on the page.
        totals = [0, 0, 0]
        n = 0
        for i in range(0, len(data) - 2, 3):
            if (data[i] >> 4, data[i + 1] >> 4, data[i + 2] >> 4) == winner:
                totals[0] += data[i]
                totals[1] += data[i + 1]
                totals[2] += data[i + 2]
                n += 1
        if not n:
            return (1, 1, 1)
        return tuple((t / n) / 255 for t in totals)
    except Exception:
        return (1, 1, 1)


# A word the reader scored below this is worth a second look. Tesseract's
# scale is 0-100; body text on a clean scan sits in the 90s, so 60 separates
# "probably fine" from "check this" without flagging half the page.
_OCR_DOUBT_BELOW = 60.0


def _ocr_skew(lines):
    """Page skew in degrees, from the slope of the recognised baselines.

    Free information: the reader already reports where each line rests, and a
    tilted page tilts them all. Reported rather than acted on, because a scan
    that is already straight must not be rotated on a noisy estimate.
    """
    import math
    angles = []
    for line in lines:
        b = line.get("baseline") or {}
        if not b.get("has_baseline"):
            continue
        try:
            dx = float(b["x1"]) - float(b["x0"])
            dy = float(b["y1"]) - float(b["y0"])
        except (KeyError, TypeError, ValueError):
            continue
        if dx < 80:          # short lines say little about the page angle
            continue
        angles.append(math.degrees(math.atan2(dy, dx)))
    if not angles:
        return 0.0
    angles.sort()
    return angles[len(angles) // 2]


def set_page_rotation(doc_id, pno, degrees):
    """Turn a page upright.

    Only the page's /Rotate entry changes, so nothing is re-encoded and no
    quality is lost -- which matters for a scan, where re-rendering the image
    to straighten it would degrade the very thing being read.
    """
    page = _doc(doc_id)[int(pno)]
    page.set_rotation((page.rotation + int(degrees)) % 360)
    return page.rotation


def ocr_doubts(doc_id, pno):
    """Where the reader was unsure, so the editor can point at those words."""
    return json.dumps(_OCR_DOUBTS.get((doc_id, int(pno))) or [])


def _ocr_word_size(font, text, width_pt, fallback):
    """Font size at which `text` is exactly `width_pt` wide.

    Fitting the width is how the OCR layer earns its alignment. Sizing from
    the word's ink height instead -- what this used to do -- reads the
    x-height of "one", the cap height of "NON" and the full ascender-to-
    descender span of "Gujarat" as if they were the same measurement, so
    neighbouring words on one line came out at noticeably different sizes and
    none of them matched the scan.
    """
    if not text or width_pt <= 0:
        return fallback
    try:
        unit = font.text_length(text, 10)
    except Exception:
        return fallback
    if unit <= 0:
        return fallback
    return 10.0 * width_pt / unit


def insert_ocr_layer(doc_id, pno, payload_json, img_width, img_height):
    """Insert recognised words as invisible, selectable text.

    The payload carries Tesseract output in image-pixel coordinates, either as
    lines of words (preferred) or a flat word list. Text is drawn with
    render_mode=3 so the page still *looks* like the scan while being
    searchable, copyable and -- since the editor reads these spans -- editable.

    Two things decide whether the layer lands on the ink:

    * the baseline. Tesseract reports one per word and per line; using it is
      exact, where deriving it from the bottom of the ink box is only right
      for words that happen to have no descender.
    * the size, fitted to each word's width (see above), then made uniform
      across the line so one odd fragment cannot leave a line ragged.
    """
    page = _doc(doc_id)[int(pno)]
    payload = json.loads(payload_json) if isinstance(payload_json, str) else payload_json
    sx = page.rect.width / float(img_width)
    sy = page.rect.height / float(img_height)
    # The page origin is not always (0, 0): a cropped page shifts everything,
    # and text placed without the offset lands off the ink by that margin.
    ox, oy = page.rect.x0, page.rect.y0

    # Accept either shape, so a caller passing a plain word list still works.
    if isinstance(payload, dict):
        lines = payload.get("lines") or []
    elif payload and isinstance(payload[0], dict) and "words" in payload[0]:
        lines = payload
    else:
        lines = [{"words": payload or []}]

    font = pymupdf.Font("helv")
    pw, ph = page.rect.width, page.rect.height
    added = 0
    doubts = []
    _OCR_PAGES.setdefault(doc_id, set()).add(int(pno))

    for line in lines:
        words = [w for w in (line.get("words") or []) if (w.get("text") or "").strip()]
        if not words:
            continue
        line_base = ((line.get("baseline") or {}).get("y0")
                     if (line.get("baseline") or {}).get("has_baseline") else None)

        placed = []
        for word in words:
            text = (word.get("text") or "").strip()
            b = word.get("bbox") or {}
            x0 = float(b.get("x0", 0)) * sx + ox
            x1 = float(b.get("x1", 0)) * sx + ox
            y0 = float(b.get("y0", 0)) * sy + oy
            y1 = float(b.get("y1", 0)) * sy + oy
            wb = word.get("baseline") or {}
            base_px = wb.get("y0") if wb.get("has_baseline") else line_base
            if base_px is None:
                # No baseline reported: the bottom of the ink is the closest
                # honest guess, nudged up for a possible descender.
                baseline = y1 - (y1 - y0) * 0.18
            else:
                baseline = float(base_px) * sy + oy
            size = _ocr_word_size(font, text, x1 - x0, max(y1 - y0, 1) * 0.95)
            placed.append({"text": text, "x0": x0, "x1": x1, "y0": y0, "y1": y1,
                           "baseline": baseline, "size": size,
                           "conf": word.get("confidence")})

        # One size for the line. The median resists a stray mark or a single
        # character, whose width says little about the type around it.
        sizes = sorted(p["size"] for p in placed if len(p["text"]) > 1) or \
                sorted(p["size"] for p in placed)
        line_size = sizes[len(sizes) // 2]

        for p in placed:
            size = line_size
            # Keep a word inside its own box: at the shared size a long word
            # could otherwise run over its neighbour and scramble selection.
            width = max(p["x1"] - p["x0"], 0.1)
            try:
                if font.text_length(p["text"], size) > width * 1.15:
                    size = _ocr_word_size(font, p["text"], width, size)
            except Exception:
                pass
            if size <= 0.4:
                continue
            page.insert_text((p["x0"], p["baseline"]), p["text"],
                             fontsize=size, fontname="helv", render_mode=3)
            added += 1
            conf = p.get("conf")
            if conf is not None and conf < _OCR_DOUBT_BELOW:
                doubts.append({
                    "text": p["text"], "conf": round(float(conf), 1),
                    "xFrac": p["x0"] / pw, "yFrac": p["y0"] / ph,
                    "wFrac": max(p["x1"] - p["x0"], 0.5) / pw,
                    "hFrac": max(p["y1"] - p["y0"], 0.5) / ph,
                })

    _OCR_DOUBTS[(doc_id, int(pno))] = doubts
    return json.dumps({"added": added, "doubts": len(doubts),
                       "skew": round(_ocr_skew(lines), 3)})


# --------------------------------------------------------------------------
# export & convert
# --------------------------------------------------------------------------

def _xlsx_col(idx):
    """0-based column index -> Excel column letters (0 -> A, 26 -> AA)."""
    s = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        s = chr(65 + rem) + s
    return s


def _xlsx_cell_value(raw):
    """Classify a table cell as numeric or text for xlsx export.

    Deliberately conservative: a value like "0123" or "00501" (an id or a
    postal code, not a quantity) is kept as text, because turning it into a
    real number would silently drop the leading zero -- a wrong-but-plausible
    number is worse than an unformatted string.
    """
    import re
    text = "" if raw is None else str(raw).replace("\r", " ").replace("\n", " ").strip()
    if not text:
        return False, text, None
    if not re.fullmatch(r"-?\d{1,3}(?:,\d{3})*(?:\.\d+)?|-?\d+(?:\.\d+)?", text):
        return False, text, None
    bare = text.replace(",", "")
    digits = bare.lstrip("-")
    if len(digits) > 1 and digits[0] == "0" and digits[1] != ".":
        return False, text, None
    return True, text, bare


_XLSX_STYLES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2">
<font><sz val="11"/><color rgb="FF1F2427"/><name val="Calibri"/></font>
<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>
</fonts>
<fills count="4">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FF21808D"/><bgColor indexed="64"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFF3F6F6"/><bgColor indexed="64"/></patternFill></fill>
</fills>
<borders count="2">
<border><left/><right/><top/><bottom/><diagonal/></border>
<border><left style="thin"><color rgb="FFD8DEE2"/></left><right style="thin"><color rgb="FFD8DEE2"/></right><top style="thin"><color rgb="FFD8DEE2"/></top><bottom style="thin"><color rgb="FFD8DEE2"/></bottom><diagonal/></border>
</borders>
<numFmts count="1"><numFmt numFmtId="164" formatCode="#,##0.00"/></numFmts>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="6">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf>
<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1"/>
<xf numFmtId="0" fontId="0" fillId="3" borderId="1" xfId="0" applyFill="1" applyBorder="1"/>
<xf numFmtId="164" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1" applyAlignment="1"><alignment horizontal="right"/></xf>
<xf numFmtId="164" fontId="0" fillId="3" borderId="1" xfId="0" applyNumberFormat="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="right"/></xf>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


def _build_xlsx(sheets, creator="MiyeePDF"):
    """Build a genuine .xlsx workbook, one worksheet per entry in `sheets`.

    sheets: [{"name": str, "rows": [[cell, ...], ...]}], first row of each
    treated as its header. Pure stdlib (zipfile) so the wasm bundle needs no
    extra vendored package for something a zip of XML files can do on its
    own -- openpyxl and its dependency are not part of the Pyodide package
    set, and PyPI wheels can't be fetched cross-origin from the browser
    (same reason PyMuPDF itself is vendored rather than pip-installed).
    Returns bytes.
    """
    import re
    import zipfile

    def esc(s):
        return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace('"', "&quot;"))

    shared = []
    shared_index = {}

    def sst(text):
        if text not in shared_index:
            shared_index[text] = len(shared)
            shared.append(text)
        return shared_index[text]

    sheet_xmls = []
    sheet_names = []
    used_names = set()
    for sheet in sheets:
        name = re.sub(r'[\[\]\*\?/\\:]', ' ', sheet["name"]).strip()[:31] or "Sheet"
        base, n = name, 2
        while name.lower() in used_names:
            suffix = f" ({n})"
            name = base[:31 - len(suffix)] + suffix
            n += 1
        used_names.add(name.lower())
        sheet_names.append(name)

        rows = sheet["rows"] or [[]]
        ncols = max((len(r) for r in rows), default=1) or 1
        widths = [8.0] * ncols
        row_xml = []
        for r_idx, row in enumerate(rows):
            is_header = r_idx == 0
            cells_xml = []
            for c_idx in range(ncols):
                raw = row[c_idx] if c_idx < len(row) else ""
                is_num, text, num_str = _xlsx_cell_value(raw)
                widths[c_idx] = min(max(widths[c_idx], len(text) + 2), 60)
                ref = f"{_xlsx_col(c_idx)}{r_idx + 1}"
                if is_header:
                    style = 1
                elif is_num:
                    style = 5 if r_idx % 2 else 4
                else:
                    style = 3 if r_idx % 2 else 2
                if is_num and not is_header:
                    cells_xml.append(f'<c r="{ref}" s="{style}"><v>{num_str}</v></c>')
                else:
                    idx = sst(text)
                    cells_xml.append(f'<c r="{ref}" t="s" s="{style}"><v>{idx}</v></c>')
            row_xml.append(f'<row r="{r_idx + 1}">' + "".join(cells_xml) + "</row>")

        cols_xml = "".join(
            f'<col min="{i + 1}" max="{i + 1}" width="{w:.2f}" customWidth="1"/>'
            for i, w in enumerate(widths)
        )
        dim_ref = f"A1:{_xlsx_col(ncols - 1)}{len(rows)}"
        sheet_xmls.append(
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<dimension ref="{dim_ref}"/>'
            '<sheetViews><sheetView workbookViewId="0">'
            '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
            '</sheetView></sheetViews>'
            f'<cols>{cols_xml}</cols>'
            f'<sheetData>{"".join(row_xml)}</sheetData>'
            '</worksheet>'
        )

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
        + "".join(
            f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for i in range(len(sheet_xmls))
        )
        + '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        + '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        + '</Types>'
    )

    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
        '</Relationships>'
    )

    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets>'
        + "".join(
            f'<sheet name="{esc(n)}" sheetId="{i + 1}" r:id="rId{i + 1}"/>'
            for i, n in enumerate(sheet_names)
        )
        + '</sheets></workbook>'
    )

    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(
            f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i + 1}.xml"/>'
            for i in range(len(sheet_xmls))
        )
        + f'<Relationship Id="rId{len(sheet_xmls) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        + f'<Relationship Id="rId{len(sheet_xmls) + 2}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
        + '</Relationships>'
    )

    shared_strings_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{len(shared)}" uniqueCount="{len(shared)}">'
        + "".join(f'<si><t xml:space="preserve">{esc(t)}</t></si>' for t in shared)
        + '</sst>'
    )

    core_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<dc:creator>{esc(creator)}</dc:creator>'
        f'<dc:title>{esc(sheet_names[0] if sheet_names else "Export")}</dc:title>'
        '</cp:coreProperties>'
    )
    app_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
        f'<Application>{esc(creator)}</Application>'
        '</Properties>'
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("xl/workbook.xml", workbook_xml)
        z.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        z.writestr("xl/styles.xml", _XLSX_STYLES_XML)
        z.writestr("xl/sharedStrings.xml", shared_strings_xml)
        for i, sx in enumerate(sheet_xmls):
            z.writestr(f"xl/worksheets/sheet{i + 1}.xml", sx)
        z.writestr("docProps/core.xml", core_xml)
        z.writestr("docProps/app.xml", app_xml)
    return buf.getvalue()


# --------------------------------------------------------------------------
# reading a signature that is already on a document
# --------------------------------------------------------------------------

def _der_length(blob):
    """How many bytes the DER object at the start of blob occupies.

    A signature sits in a slot padded with zeros, so the blob handed back is
    longer than the signature in it. Reading the length header is the only
    way to know where it really ends - trimming trailing zeros would eat a
    legitimate one.
    """
    if len(blob) < 2 or blob[0] != 0x30:
        return len(blob)
    first = blob[1]
    if first < 0x80:
        return 2 + first
    count = first & 0x7F
    if count == 0 or len(blob) < 2 + count:
        return len(blob)
    return 2 + count + int.from_bytes(blob[2:2 + count], "big")


def verify_signatures(data):
    """Every signature in a PDF, and what each one covers.

    The certificate arithmetic is finished in the browser, where the
    certificate parser already lives. What only the file can answer is done
    here: which bytes each signature covers, whether anything was added
    afterwards, and what those bytes hash to. The caller compares that hash
    with the one inside the signature - if they differ, the document changed
    after it was signed.
    """
    raw = bytes(data)
    signatures = []

    for match in re.finditer(rb"/ByteRange\s*\[([^\]]*)\]", raw):
        try:
            nums = [int(n) for n in match.group(1).split()]
        except ValueError:
            continue
        if len(nums) != 4:
            continue
        start1, len1, start2, len2 = nums
        # A placeholder that was never filled in has a byte range of zeros or
        # of the padding digits; anything that does not describe real spans of
        # this file is not a signature to report on.
        if (start1 != 0 or len1 <= 0 or len2 < 0
                or start2 < start1 + len1 or start2 + len2 > len(raw)):
            continue
        hole = raw[start1 + len1:start2]
        if not (hole.startswith(b"<") and hole.endswith(b">")):
            continue

        digits = re.sub(r"[^0-9A-Fa-f]", "", hole[1:-1].decode("latin-1"))
        blob = bytes.fromhex(digits[:len(digits) - len(digits) % 2])
        blob = blob[:_der_length(blob)]
        if not blob:
            continue                  # an empty slot: prepared, never signed

        covered = raw[start1:start1 + len1] + raw[start2:start2 + len2]
        signatures.append({
            "byteRange": nums,
            "coveredTo": start2 + len2,
            "fileSize": len(raw),
            # Anything past the covered range was added after this signature:
            # another signature, a form filled in, or an edit.
            "coversWholeFile": start2 + len2 == len(raw),
            "cms": blob.hex(),
            "digests": {
                "sha1": hashlib.sha1(covered).hexdigest(),
                "sha256": hashlib.sha256(covered).hexdigest(),
                "sha384": hashlib.sha384(covered).hexdigest(),
                "sha512": hashlib.sha512(covered).hexdigest(),
            },
            "field": "", "name": "", "reason": "", "location": "",
            "signedAt": "", "subFilter": "",
        })

    # What the document itself says about those fields. It is only a label -
    # the name in a signature dictionary is typed by whoever signed and is
    # not evidence of anything - but it is what a reader shows, so show it too.
    if signatures:
        try:
            doc = pymupdf.open(stream=raw, filetype="pdf")
            if not doc.needs_pass:
                for page in doc:
                    for widget in page.widgets():
                        if widget.field_type != pymupdf.PDF_WIDGET_TYPE_SIGNATURE:
                            continue
                        value = doc.xref_get_key(widget.xref, "V")
                        if value[0] != "xref":
                            continue
                        sig_xref = int(value[1].split()[0])
                        spread = doc.xref_get_key(sig_xref, "ByteRange")
                        if spread[0] != "array":
                            continue
                        try:
                            nums = [int(n) for n in spread[1].strip("[]").split()]
                        except ValueError:
                            continue
                        for entry in signatures:
                            if entry["byteRange"] != nums:
                                continue
                            entry["field"] = widget.field_name or ""
                            for key, into in (("Name", "name"), ("Reason", "reason"),
                                              ("Location", "location"), ("M", "signedAt"),
                                              ("SubFilter", "subFilter")):
                                got = doc.xref_get_key(sig_xref, key)
                                if got[0] in ("string", "name"):
                                    entry[into] = got[1].lstrip("/")
            doc.close()
        except Exception:
            pass                      # metadata is a nicety; the maths is not

    signatures.sort(key=lambda s: s["coveredTo"])
    return json.dumps({
        "signatures": signatures,
        "fileSize": len(raw),
        # Each save appends a revision. More than one is normal for a signed
        # document - signing is itself a revision - but it is worth knowing.
        "revisions": raw.count(b"%%EOF"),
    })


def export_text(doc_id):
    return "\n\n".join(page.get_text() for page in _doc(doc_id))


def export_html(doc_id):
    """HTML that Word opens with layout largely intact."""
    doc = _doc(doc_id)
    body = "\n".join(
        f'<div class="pdf-page" style="position:relative">{page.get_text("html")}</div>'
        for page in doc
    )
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<style>.pdf-page{page-break-after:always;margin-bottom:24px}</style>"
        f"</head><body>{body}</body></html>"
    )


_DOCX_STYLES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:docDefaults><w:rPrDefault><w:rPr>
<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri"/><w:sz w:val="22"/>
</w:rPr></w:rPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal">
<w:name w:val="Normal"/>
<w:pPr><w:spacing w:after="120" w:line="264" w:lineRule="auto"/></w:pPr>
</w:style>
<w:style w:type="paragraph" w:styleId="Title">
<w:name w:val="Title"/><w:basedOn w:val="Normal"/>
<w:pPr><w:spacing w:before="240" w:after="160"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="48"/><w:color w:val="1F2427"/></w:rPr>
</w:style>
<w:style w:type="paragraph" w:styleId="Heading1">
<w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>
<w:pPr><w:outlineLvl w:val="0"/><w:spacing w:before="240" w:after="120"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="32"/><w:color w:val="21808D"/></w:rPr>
</w:style>
<w:style w:type="paragraph" w:styleId="Heading2">
<w:name w:val="heading 2"/><w:basedOn w:val="Normal"/>
<w:pPr><w:outlineLvl w:val="1"/><w:spacing w:before="200" w:after="100"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="26"/><w:color w:val="21808D"/></w:rPr>
</w:style>
</w:styles>"""


def export_docx(doc_id):
    """Export to a genuine .docx.

    The previous Word export wrote an HTML payload under a .doc extension,
    which makes Word open it with a "the format and extension don't match"
    warning and leaves the result awkward to edit. This builds real
    WordprocessingML instead, carrying over the things that survive the trip
    usefully: paragraph breaks, bold and italic, relative text size, and page
    breaks. Layout-exact conversion is not the goal -- an editable document is.
    """
    import zipfile

    def esc(s):
        return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    doc = _doc(doc_id)

    # Body size is the most common span size in the document; headings are
    # judged relative to it, so a document set in 9pt is not treated as one
    # long heading just because another is set in 12pt.
    sizes = {}
    for page in doc:
        for block in page.get_text("dict").get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    if span.get("text", "").strip():
                        key = round(float(span.get("size", 11)), 1)
                        sizes[key] = sizes.get(key, 0) + len(span["text"])
    body_size = max(sizes, key=sizes.get) if sizes else 11.0

    paragraphs = []
    for pno, page in enumerate(doc):
        if pno:
            paragraphs.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
        for block in page.get_text("dict").get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                runs = []
                largest = 0.0
                for span in line.get("spans", []):
                    text = span.get("text", "")
                    if not text:
                        continue
                    size = float(span.get("size", body_size))
                    largest = max(largest, size)
                    flags = int(span.get("flags", 0))
                    props = []
                    if flags & 16:
                        props.append("<w:b/>")
                    if flags & 2:
                        props.append("<w:i/>")
                    props.append(f'<w:sz w:val="{max(2, int(round(size * 2)))}"/>')
                    runs.append(
                        f'<w:r><w:rPr>{"".join(props)}</w:rPr>'
                        f'<w:t xml:space="preserve">{esc(text)}</w:t></w:r>'
                    )
                if not runs:
                    continue
                if largest >= body_size * 1.8:
                    style = "Title"
                elif largest >= body_size * 1.4:
                    style = "Heading1"
                elif largest >= body_size * 1.15:
                    style = "Heading2"
                else:
                    style = "Normal"
                style_xml = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>'
                paragraphs.append(f"<w:p>{style_xml}{''.join(runs)}</w:p>")

    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f'<w:body>{"".join(paragraphs)}'
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134"/></w:sectPr>'
        '</w:body></w:document>'
    )

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        '</Types>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
        '</Relationships>'
    )
    doc_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        '</Relationships>'
    )
    meta = doc.metadata or {}
    core_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<dc:title>{esc(meta.get("title") or "")}</dc:title>'
        f'<dc:creator>{esc(meta.get("author") or "MiyeePDF")}</dc:creator>'
        '</cp:coreProperties>'
    )
    app_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
        '<Application>MiyeePDF</Application></Properties>'
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("word/document.xml", document_xml)
        z.writestr("word/_rels/document.xml.rels", doc_rels)
        z.writestr("word/styles.xml", _DOCX_STYLES_XML)
        z.writestr("docProps/core.xml", core_xml)
        z.writestr("docProps/app.xml", app_xml)
    return base64.b64encode(buf.getvalue()).decode()


def _text_lines(page, clip=None, top=None):
    """Words on the page grouped into the visual lines they sit on."""
    words = page.get_text("words", clip=clip)
    if top is not None:
        words = [w for w in words if (w[1] + w[3]) / 2.0 > top]
    if not words:
        return []
    words.sort(key=lambda w: ((w[1] + w[3]) / 2.0, w[0]))
    lines = []
    for w in words:
        mid = (w[1] + w[3]) / 2.0
        if lines and mid - lines[-1]["mid"] <= max((w[3] - w[1]) * 0.6, 2.0):
            lines[-1]["words"].append(w)
            lines[-1]["mid"] = (lines[-1]["mid"] + mid) / 2.0
        else:
            lines.append({"mid": mid, "words": [w]})
    for line in lines:
        line["words"].sort(key=lambda w: w[0])
    return lines


def _column_gutters(lines, tolerance):
    """The vertical lanes of whitespace that run down a block of text.

    Where a page draws no rules, the columns are held apart by the fact that
    no line ever puts ink between them. Projecting every word onto the x axis
    and looking for the stripes nothing reaches finds those lanes, and unlike
    measuring the gaps line by line it works whether a column is aligned left
    (a narration) or right (an amount) - a statement has both.

    A single overlong cell is allowed to cross a lane rather than erasing it,
    which is what `tolerance` is for.
    """
    left = min(w[0] for line in lines for w in line["words"])
    right = max(w[2] for line in lines for w in line["words"])
    if right - left < 20:
        return None

    width = int(right - left) + 2
    # Counted as intervals rather than bin by bin. Walking every point a word
    # covers is a million steps for a thirty-six page statement, and this runs
    # in WebAssembly where a Python-level loop is the expensive thing: the
    # same page took 54 seconds that way and under a second this way. Each
    # line contributes its own merged spans, so a lane is judged by how many
    # lines cross it, not how many words.
    delta = [0] * (width + 1)
    for line in lines:
        spans = sorted((max(int(w[0] - left), 0), min(int(w[2] - left) + 1, width))
                       for w in line["words"])
        merged_lo, merged_hi = None, None
        for lo, hi in spans:
            if merged_hi is not None and lo <= merged_hi:
                merged_hi = max(merged_hi, hi)
                continue
            if merged_hi is not None:
                delta[merged_lo] += 1
                delta[merged_hi] -= 1
            merged_lo, merged_hi = lo, hi
        if merged_hi is not None:
            delta[merged_lo] += 1
            delta[merged_hi] -= 1

    ink = [0] * width
    running = 0
    for b in range(width):
        running += delta[b]
        ink[b] = running

    spans, run_start = [], None
    for b in range(width):
        empty = ink[b] <= tolerance
        if empty and run_start is None:
            run_start = b
        elif not empty and run_start is not None:
            if b - run_start >= 3:
                spans.append((run_start, b))
            run_start = None
    if run_start is not None and width - run_start >= 3:
        spans.append((run_start, width))

    # Column edges: the page margin, the middle of each lane, the far margin.
    edges = [left - 1.0]
    for a, b in spans:
        if a == 0 or b >= width:
            continue                      # the margins, not a gutter
        edges.append(left + (a + b) / 2.0)
    edges.append(right + 1.0)
    return edges if len(edges) >= 3 else None


def _header_groups(line):
    """The header line's own headings, as (start, end) pairs.

    A heading is one or more words with ordinary spacing between them
    ("Value Date"); the wider gap to the next heading is what separates the
    columns.
    """
    words = line["words"]
    if len(words) < 2:
        return []
    # The spacing is taken from the type size, not from the gaps on the line:
    # on a header row every gap is a column gap, so the median of them is a
    # column width and splits nothing.
    height = sorted(w[3] - w[1] for w in words)[len(words) // 2]
    limit = max(height * 0.9, 6.0)
    groups, start, end = [], words[0][0], words[0][2]
    for prev, w in zip(words, words[1:]):
        if w[0] - prev[2] > limit:
            groups.append((start, end))
            start = w[0]
        end = w[2]
    groups.append((start, end))
    return groups


def _split_by_header(lines, edges):
    """Split any column that the header shows is really two.

    Whitespace lanes fail where something crosses them - a section heading
    spanning the page, or a speck OCR left behind - and two columns then
    arrive merged into one. The header row does not lie about how many
    columns there are, so where it puts two headings inside a single column,
    that column is divided between them.
    """
    header = max(lines, key=lambda ln: len(_header_groups(ln)))
    groups = _header_groups(header)
    if len(groups) < 3:
        return edges

    extra = []
    for lo, hi in zip(edges, edges[1:]):
        inside = [g for g in groups if lo <= g[0] < hi]
        for left, right in zip(inside, inside[1:]):
            extra.append((left[1] + right[0]) / 2.0)
    if not extra:
        return edges
    return sorted(set(edges) | set(extra))


def _rows_in_block(lines, edges):
    """Lay a block's words out into the columns the gutters mark."""
    ncols = len(edges) - 1
    rows = []
    for line in lines:
        cells = [[] for _ in range(ncols)]
        for w in line["words"]:
            centre = (w[0] + w[2]) / 2.0
            col = ncols - 1
            for i in range(ncols):
                if edges[i] <= centre < edges[i + 1]:
                    col = i
                    break
            cells[col].append(w[4])
        row = [" ".join(c).strip() for c in cells]
        if any(row):
            rows.append(row)
    return rows


def _borderless_tables(page):
    """Find tables on a page that draws no lines at all.

    Bank and deposit statements printed from a web page are the common case:
    the columns are held apart by whitespace and a background tint, and once
    such a page has been scanned and read back by OCR even the tint is gone,
    so nothing but the layout is left to go on.
    """
    lines = _text_lines(page)
    if len(lines) < 3:
        return []

    # Break the page into blocks at a wide vertical gap, so a heading or an
    # address panel is not folded in with the table beneath it - each block
    # gets its own columns, because they genuinely differ.
    gaps = sorted(b["mid"] - a["mid"] for a, b in zip(lines, lines[1:]))
    spacing = gaps[len(gaps) // 2] if gaps else 12.0
    blocks, current = [], []
    for i, line in enumerate(lines):
        if i and (line["mid"] - lines[i - 1]["mid"]) > spacing * 2.2:
            if len(current) >= 3:
                blocks.append(current)
            current = []
        current.append(line)
    if len(current) >= 3:
        blocks.append(current)

    tables = []
    for block in blocks:
        edges = _column_gutters(block, tolerance=max(1, len(block) // 6))
        if not edges or len(edges) - 1 < 3:
            continue
        edges = _split_by_header(block, edges)
        rows = _rows_in_block(block, edges)
        # Only a block whose lines really do span its columns is a table; a
        # paragraph that happens to have a ragged edge is not.
        spanning = sum(1 for r in rows if sum(1 for c in r if c) >= 3)
        if len(rows) >= 3 and spanning >= max(2, len(rows) * 0.5):
            tables.append(rows)
    return tables


def _find_page_tables(page, text_fallback=True):
    """Detect tables on a page: ruled borders first, then column alignment.

    Most PDFs export from Word/Excel/accounting software draw no ruling lines
    at all -- the "table" is just text lined up in columns -- so a
    ruled-only detector misses almost everything. Falling back to the
    text-alignment strategy only when ruled detection finds nothing keeps
    the common (ruled) case exact while still catching those.
    """
    try:
        found = list(page.find_tables().tables)
    except Exception:
        found = []
    if found or not text_fallback:
        return found
    # The text strategy is expensive - it compares every character against
    # every candidate cell, which on a thirty-six page statement is twenty
    # million comparisons and three quarters of the whole export. It is worth
    # that only when nothing else has found anything, so the caller decides.
    try:
        found = list(page.find_tables(
            vertical_strategy="text", horizontal_strategy="text",
        ).tables)
    except Exception:
        found = []
    return found


_AMOUNT_RE = None
_DATE_RE = None


def _looks_like_value(text):
    """True for a date or a money figure - the things that start a record.

    Used to tell a wrapped description ("BHAILALBHAI CHHATBAR" on its own
    line) from a genuine new row that simply has no date, such as a closing
    balance or a total.
    """
    global _AMOUNT_RE, _DATE_RE
    import re
    if _AMOUNT_RE is None:
        _AMOUNT_RE = re.compile(r"^[(\-]?[\d,]+\.?\d*[)]?$")
        _DATE_RE = re.compile(r"\d{1,4}[-/][A-Za-z\d]{2,3}[-/]\d{2,4}")
    t = text.strip()
    if not t:
        return False
    return bool(_DATE_RE.search(t)) or bool(_AMOUNT_RE.match(t))


def _column_bounds(table):
    """Vertical dividing lines between columns, taken from the header cells.

    The header is the one row the ruling does get right, and its cells sit
    edge to edge, so the boundary between two columns is the edge they share.
    """
    cells = [c for c in (table.header.cells if table.header else []) if c]
    if len(cells) < 2:
        return None
    bounds = [table.bbox[0] - 1.0]
    for left, right in zip(cells, cells[1:]):
        bounds.append((left[2] + right[0]) / 2.0)
    bounds.append(table.bbox[2] + 1.0)
    return bounds


def _rows_from_words(page, table):
    """Rebuild a table's rows from where the words actually sit.

    A bank or fund statement typically rules its header and then draws
    nothing between the rows beneath it. A ruling-based reader sees one row
    and returns every transaction crammed into single cells - twenty-one
    payments arriving as one line of an Excel sheet. Reading positions
    instead recovers the rows the page shows.

    A line with no date or figure in it is treated as the wrapped remainder
    of the line above rather than a row of its own, which is what a long
    payee name is; a line that does carry one, such as a closing balance
    with no date, stays a row.
    """
    bounds = _column_bounds(table)
    if not bounds:
        return None
    ncols = len(bounds) - 1
    header_bottom = max((c[3] for c in table.header.cells if c), default=table.bbox[1])

    words = [w for w in page.get_text("words", clip=table.bbox)
             if (w[1] + w[3]) / 2.0 > header_bottom]
    if not words:
        return None

    # Group into visual lines: a word belongs to the line it vertically
    # overlaps, which keeps a superscript or a slightly raised glyph on its
    # own line rather than starting a new row.
    words.sort(key=lambda w: ((w[1] + w[3]) / 2.0, w[0]))
    lines = []
    for w in words:
        mid = (w[1] + w[3]) / 2.0
        if lines and mid - lines[-1]["mid"] <= max((w[3] - w[1]) * 0.6, 2.0):
            lines[-1]["words"].append(w)
            lines[-1]["mid"] = (lines[-1]["mid"] + mid) / 2.0
        else:
            lines.append({"mid": mid, "words": [w]})

    raw = []
    for line in lines:
        cells = [[] for _ in range(ncols)]
        for w in sorted(line["words"], key=lambda w: w[0]):
            centre = (w[0] + w[2]) / 2.0
            col = ncols - 1
            for i in range(ncols):
                if bounds[i] <= centre < bounds[i + 1]:
                    col = i
                    break
            cells[col].append(w[4])
        row = [" ".join(c).strip() for c in cells]
        if any(row):
            raw.append(row)
    if not raw:
        return None

    # Which columns carry dates and figures, judged over the whole table. A
    # line that fills none of them is the wrapped tail of the line above: a
    # long payee name, or the "2026" left over from a date range that broke
    # across two lines. A line that fills one is a row in its own right, even
    # with no date - a closing balance, or an opening one.
    value_col = []
    for i in range(ncols):
        seen = [r[i] for r in raw if r[i]]
        value_col.append(bool(seen) and
                         sum(1 for c in seen if _looks_like_value(c)) >= len(seen) * 0.6)

    rows = []
    for row in raw:
        wrapped = (not row[0]) and not any(row[i] for i in range(ncols) if value_col[i])
        if wrapped and rows:
            for i, part in enumerate(row):
                if part:
                    rows[-1][i] = (rows[-1][i] + " " + part).strip()
        else:
            rows.append(row)

    return rows or None


def _table_rows(page, table):
    """The table's rows, read from the ruling unless the ruling failed.

    Position-based reading can only find more rows than the ruling did when
    the ruling left row separators out, so more rows is the signal that it
    did. Where the ruling is complete, it stays the authority: it knows about
    merged and spanning cells that word positions cannot see.
    """
    try:
        ruled = table.extract()
    except Exception:
        ruled = None
    ruled = [[("" if c is None else str(c).replace("\n", " ").strip()) for c in row]
             for row in (ruled or [])]

    try:
        read = _rows_from_words(page, table)
    except Exception:
        read = None

    if read and len(read) >= max(len(ruled), 1) * 2:
        header = ruled[0] if ruled else [""] * len(read[0])
        return [header] + read
    return ruled


def _no_tables_reason(doc):
    """Why nothing came out - a scan and a table-less document are different
    problems, and telling someone "no tables were detected" about a scan
    sends them hunting a bug that is not there."""
    blank = sum(1 for page in doc if not page.get_text().strip())
    if blank == doc.page_count:
        return ("This PDF is a scan: its pages are images, so there is no text "
                "to pull a table from. Run OCR on it first, then export again.")
    if blank:
        return (f"No tables were detected. {blank} of {doc.page_count} page(s) are "
                "scans with no text in them - run OCR on it first if the table is "
                "on one of those.")
    return "No tables were detected in this PDF."


def _page_tables_as_rows(page):
    """Every table on a page, as plain lists of rows.

    Ruling is used where a page has it, because it knows about merged and
    spanning cells that layout alone cannot show. Only when that finds
    nothing worth having does the layout reader run - which is the case for a
    statement printed from a web page, and for any scan, where no ruling
    survives to be read.
    """
    # Ruling first, because it is cheap and authoritative where it exists;
    # then the layout reader, which is also cheap. The costly text strategy is
    # only reached if both come back with nothing.
    ruled = []
    for table in _find_page_tables(page, text_fallback=False):
        rows = _table_rows(page, table)
        if rows:
            ruled.append(rows)
    loose = _borderless_tables(page)
    if not ruled and not loose:
        for table in _find_page_tables(page):
            rows = _table_rows(page, table)
            if rows:
                ruled.append(rows)

    # Neither reader is right often enough to be trusted on a threshold. A
    # detector that has misread a page gives itself away by the shape of what
    # it produces - a grid of mostly empty cells, or two columns where the
    # page plainly has seven - so both are scored and the better one wins.
    if not loose:
        chosen = ruled
    elif not ruled:
        chosen = loose
    else:
        chosen = loose if _table_score(loose) > _table_score(ruled) else ruled
    return [t for t in (_tidy_table(rows) for rows in chosen) if t]


def _tidy_table(rows):
    """Drop what a layout reader leaves behind but a spreadsheet should not.

    Reading a page by position produces a column wherever the page happened
    to leave a gap, so a table arrives with empty columns down its side and
    a stray column holding nothing but the "%" that follows every rate. The
    figures are right either way; the sheet is only usable without them.
    """
    if not rows:
        return rows
    width = max(len(r) for r in rows)
    grid = [list(r) + [""] * (width - len(r)) for r in rows]

    # A column carrying one short token and nothing else is a unit, not a
    # column: it belongs on the end of the value it qualifies.
    for i in range(width - 1, 0, -1):
        values = {str(grid[r][i]).strip() for r in range(len(grid)) if str(grid[r][i]).strip()}
        if len(values) == 1 and len(next(iter(values))) <= 2:
            unit = next(iter(values))
            for row in grid:
                if str(row[i]).strip():
                    row[i - 1] = (str(row[i - 1]).strip() + " " + unit).strip()
                    row[i] = ""

    keep = [i for i in range(width) if any(str(r[i]).strip() for r in grid)]
    tidy = [[r[i] for i in keep] for r in grid]
    return [r for r in tidy if any(str(c).strip() for c in r)]


def _table_score(tables):
    """How much a set of extracted tables looks like real tables.

    Density is the signal: a correct grid is mostly full, while a misread one
    scatters a few words across a wide grid of blanks. Width is worth a
    little too, since collapsing seven columns into two loses information
    that no amount of density makes up for.
    """
    cells = filled = 0
    widest = 0
    for rows in tables:
        if not rows:
            continue
        width = max(len(r) for r in rows)
        widest = max(widest, width)
        for row in rows:
            cells += width
            filled += sum(1 for c in row if str(c).strip())
    if not cells:
        return 0.0
    return (filled / cells) * min(widest, 12) / 12.0


def export_tables(doc_id):
    """Detect tables and return them as CSV files."""
    doc = _doc(doc_id)
    out = []
    for pno, page in enumerate(doc):
        for tno, rows in enumerate(_page_tables_as_rows(page), 1):
            if not rows:
                continue
            buf = io.StringIO()
            writer = csv.writer(buf)
            for row in rows:
                writer.writerow(["" if c is None else str(c).replace("\n", " ") for c in row])
            out.append({
                "name": f"page{pno + 1}-table{tno}.csv",
                "rows": len(rows),
                "cols": len(rows[0]) if rows else 0,
                "csv": buf.getvalue(),
            })
    return json.dumps(out)


def _document_grid(doc, min_fill=0.4):
    """One set of columns for the whole document, and every row laid into it.

    A statement running over thirty-six pages is one table, but read page by
    page it comes back as thirty-six grids whose column counts disagree - a
    leading column that exists on page one because of a stray full stop, and
    not on page two. Stacking those loses the alignment that makes the sheet
    worth having.

    So the columns are worked out once, over every line in the document at
    once: a lane of whitespace has to be clear on nearly every line of every
    page to count, which is a far stronger signal than one page can give.
    Rows too sparse to be data - a letterhead, an address, a page number -
    are left out, since repeating them between every page's figures is what
    made the combined sheet unusable in the first place.
    """
    pooled, per_page = [], []
    for pno, page in enumerate(doc):
        lines = _text_lines(page)
        per_page.append((pno, lines))
        pooled.extend(lines)
    if len(pooled) < 3:
        return []

    edges = _column_gutters(pooled, tolerance=max(1, len(pooled) // 40))
    if not edges or len(edges) - 1 < 2:
        return []
    edges = _split_by_header(pooled, edges)
    ncols = len(edges) - 1

    out = []
    for pno, lines in per_page:
        for row in _rows_in_block(lines, edges):
            if sum(1 for c in row if str(c).strip()) >= max(2, ncols * min_fill):
                out.append([f"Page {pno + 1}"] + row)
    return out


def _combined_header(rows):
    """Turn the document's first rows into one header line.

    A column heading printed on two lines - "Original" above "Principal" -
    reads as two rows in a grid, and a spreadsheet wants it as one cell. The
    continuation is recognised by being sparser than the line above it and
    carrying no figures, so a genuine first record is never folded into the
    heading.
    """
    if not rows:
        return rows
    head = list(rows[0])
    head[0] = "Source"
    body = rows[1:]
    while body:
        nxt = body[0]
        filled_head = sum(1 for c in head[1:] if str(c).strip())
        filled_next = sum(1 for c in nxt[1:] if str(c).strip())
        if filled_next >= filled_head or any(_looks_like_value(str(c)) for c in nxt[1:]):
            break
        for i in range(1, len(head)):
            if i < len(nxt) and str(nxt[i]).strip():
                head[i] = (str(head[i]).strip() + " " + str(nxt[i]).strip()).strip()
        body = body[1:]
    return [head] + body


def _combine_tables(sheets):
    """Fold every table into one sheet.

    A thirty-six page statement is one table that happens to be printed
    across thirty-six pages, and thirty-six worksheets of it cannot be
    sorted, totalled or filtered as the single list it really is. Tables
    sharing a header are run together under one copy of it; a table with a
    different header starts a fresh block, so nothing is silently stacked
    under a heading that does not describe it.
    """
    width = max((max(len(r) for r in s["rows"]) for s in sheets if s["rows"]), default=0)
    if not width:
        return {"name": "All tables", "rows": []}

    out = []
    last_header = None
    for sheet in sheets:
        rows = [list(r) + [""] * (width - len(r)) for r in sheet["rows"] if any(str(c).strip() for c in r)]
        if not rows:
            continue
        header, body = rows[0], rows[1:]
        same = last_header is not None and [c.strip().lower() for c in header] == last_header
        if not same:
            if out:
                out.append([""] * (width + 1))     # a blank line between blocks
            out.append(["Source"] + header)
            last_header = [c.strip().lower() for c in header]
        # Every row says which page it came from, so a figure can always be
        # traced back to the page it was printed on.
        for row in body:
            out.append([sheet["name"]] + row)
    return {"name": "All tables", "rows": out}


# doc_id -> sheets gathered so far, for a workbook built from several files.
_XLSX_BATCH = {}


# --------------------------------------------------------------------------
# pictures into a document
# --------------------------------------------------------------------------

# Photographs of paper are how a great many documents arrive - a client
# sends eight pictures of a rent agreement, or a phone stands in for a
# scanner. They accumulate here until the whole set is known, because the
# page size can depend on all of them.
_IMAGE_BATCH = {}

# Points, at 72 to the inch.
_PAPER = {
    "a4": (595.28, 841.89),
    "a5": (419.53, 595.28),
    "letter": (612.0, 792.0),
    "legal": (612.0, 1008.0),
}
_MM = 72.0 / 25.4


def start_image_pdf(key):
    """Begin collecting pictures for a new document."""
    _IMAGE_BATCH[key] = []
    return True


def add_image_page(key, data, name=""):
    """Hold one picture. Returns what the engine can see in it."""
    raw = bytes(data)
    try:
        pix = pymupdf.Pixmap(raw)
        width, height = pix.width, pix.height
        pix = None
    except Exception:
        raise ValueError("IMAGE_UNREADABLE")
    _IMAGE_BATCH.setdefault(key, []).append({"raw": raw, "name": str(name)})
    return json.dumps({"ok": True, "width": width, "height": height,
                       "count": len(_IMAGE_BATCH[key])})


def finish_image_pdf(key, paper="fit", margin_mm=0, landscape=False):
    """Build the document and forget the pictures.

    "fit" gives every page the shape of its own picture, so nothing is
    letterboxed and nothing is cropped - the usual want for a phone
    photograph. A named paper size is for printing, where the pages have to
    match each other and the tray.
    """
    images = _IMAGE_BATCH.pop(key, [])
    if not images:
        raise ValueError("NO_IMAGES")

    margin = max(0.0, float(margin_mm)) * _MM
    out = pymupdf.open()
    for item in images:
        pix = pymupdf.Pixmap(item["raw"])
        wide, high = pix.width, pix.height
        pix = None
        if not wide or not high:
            continue

        if str(paper) == "fit":
            # The page takes the picture's own shape, scaled so its long edge
            # matches A4's - a photograph then prints at a sensible size
            # instead of at whatever pixel count the camera happened to use.
            long_edge = _PAPER["a4"][1]
            if wide >= high:
                page_w, page_h = long_edge, long_edge * high / wide
            else:
                page_w, page_h = long_edge * wide / high, long_edge
            page_w += 2 * margin
            page_h += 2 * margin
        else:
            page_w, page_h = _PAPER.get(str(paper), _PAPER["a4"])
            # Turn the paper to suit the picture rather than shrinking a
            # landscape photograph into a portrait page.
            wants_landscape = bool(landscape) or wide > high
            if wants_landscape:
                page_w, page_h = page_h, page_w

        page = out.new_page(width=page_w, height=page_h)
        room = pymupdf.Rect(margin, margin, page_w - margin, page_h - margin)
        if room.is_empty or room.width <= 0 or room.height <= 0:
            room = page.rect
        # Fit inside the margins without distorting: PyMuPDF keeps the aspect
        # ratio and centres what is left over.
        page.insert_image(room, stream=item["raw"], keep_proportion=True)

    if not out.page_count:
        out.close()
        raise ValueError("NO_IMAGES")
    data = out.tobytes(garbage=3, deflate=True)
    out.close()
    return data


def cancel_image_pdf(key):
    """Forget pictures collected for a document that was never built."""
    return bool(_IMAGE_BATCH.pop(key, None))


# --------------------------------------------------------------------------
# trimming margins
# --------------------------------------------------------------------------

_TRIM_SCALE = 0.5


def _ink_rows(samples, width, height, floor):
    """The first and last rows of a grey pixmap with anything on them.

    min() over a slice runs in C, so a whole row costs one call. Walking the
    pixels in Python would be far too slow for a long document under
    WebAssembly.
    """
    first, last = -1, -1
    for y in range(height):
        if min(samples[y * width:(y + 1) * width]) < floor:
            if first < 0:
                first = y
            last = y
    return first, last, height


def content_bounds(doc_id, pages_json="", tolerance=12, padding_pt=6):
    """Where the ink actually is on each page, as fractions of the page.

    Measured from a rendering rather than from the text, because on a scan
    every word belongs to one big image and the text boxes say nothing about
    the margins. The columns are read from a second rendering turned on its
    side, so both passes can use the fast row scan; a scan down the columns
    in Python would be far too slow for a long document in WebAssembly.
    """
    doc = _doc(doc_id)
    wanted = json.loads(pages_json) if pages_json else list(range(doc.page_count))
    floor = 255 - max(1, int(tolerance))
    found = {}

    # Half scale is 36 to the inch: each pixel is two points, finer than any
    # margin worth trimming, and a fortieth of the pixels of a full render.
    upright_at = pymupdf.Matrix(_TRIM_SCALE, _TRIM_SCALE)
    sideways_at = pymupdf.Matrix(_TRIM_SCALE, _TRIM_SCALE).prerotate(90)

    for pno in wanted:
        index = int(pno)
        if not 0 <= index < doc.page_count:
            continue
        page = doc[index]

        upright = page.get_pixmap(matrix=upright_at, colorspace=pymupdf.csGRAY)
        top, bottom, rows = _ink_rows(upright.samples, upright.width,
                                      upright.height, floor)
        upright = None
        if top < 0:
            found[index] = None                 # a blank page: nothing to trim
            continue

        # Turned on its side, the rows of the rendering are the columns of the
        # page, and in the same direction - measured, not assumed.
        sideways = page.get_pixmap(matrix=sideways_at, colorspace=pymupdf.csGRAY)
        left, right, cols = _ink_rows(sideways.samples, sideways.width,
                                      sideways.height, floor)
        sideways = None
        if left < 0:
            found[index] = None
            continue

        span = page.rect
        pad_x = float(padding_pt) / max(span.width, 1)
        pad_y = float(padding_pt) / max(span.height, 1)
        found[index] = {
            "x0": round(max(0.0, left / cols - pad_x), 5),
            "y0": round(max(0.0, top / rows - pad_y), 5),
            "x1": round(min(1.0, (right + 1) / cols + pad_x), 5),
            "y1": round(min(1.0, (bottom + 1) / rows + pad_y), 5),
        }

    trimmed = {k: v for k, v in found.items() if v}
    return json.dumps({
        "bounds": {str(k): v for k, v in found.items()},
        "measured": len(found),
        "blank": len(found) - len(trimmed),
    })


def start_table_batch(key):
    """Begin collecting tables from more than one document."""
    _XLSX_BATCH[key] = []
    return True


def add_to_table_batch(key, doc_id, label, combine=False):
    """Fold one document's tables into the workbook being collected.

    Four statements from the same bank are one body of work, and wanting them
    in one file is the ordinary case rather than an unusual one. Each keeps
    its own sheet, named after the file, so a figure can always be traced
    back to the document it was printed in.
    """
    doc = _doc(doc_id)
    sheets = _sheets_for(doc, combine)
    batch = _XLSX_BATCH.setdefault(key, [])
    for i, sheet in enumerate(sheets):
        # Excel refuses a sheet name over 31 characters or carrying : \ / ? * [ ]
        name = _safe_sheet_name(label if len(sheets) == 1 else f"{label} {i + 1}",
                                [s["name"] for s in batch])
        batch.append({"name": name, "rows": sheet["rows"]})
    return json.dumps({"sheets": len(batch), "added": len(sheets)})


def finish_table_batch(key, creator="MiyeePDF"):
    """Build the workbook from everything collected."""
    sheets = _XLSX_BATCH.pop(key, [])
    if not sheets:
        return json.dumps({"ok": False, "reason": "No tables were found in any of those files."})
    data = _build_xlsx(sheets, creator=creator)
    return json.dumps({
        "ok": True,
        "b64": base64.b64encode(data).decode(),
        "sheets": [{"name": s["name"], "rows": len(s["rows"]),
                    "cols": len(s["rows"][0]) if s["rows"] else 0} for s in sheets],
    })


def _safe_sheet_name(raw, taken):
    """A worksheet name Excel will actually accept, and not a duplicate."""
    name = "".join(" " if c in ":\\/?*[]" else c for c in str(raw)).strip() or "Sheet"
    name = name[:31]
    if name not in taken:
        return name
    for n in range(2, 200):
        suffix = f" ({n})"
        candidate = name[:31 - len(suffix)] + suffix
        if candidate not in taken:
            return candidate
    return name[:28] + "..."


def _sheets_for(doc, combine):
    """The sheets one document contributes, honouring the one-sheet choice."""
    sheets = []
    for pno, page in enumerate(doc):
        for tno, rows in enumerate(_page_tables_as_rows(page), 1):
            if not rows:
                continue
            clean = [["" if c is None else str(c).replace("\n", " ") for c in row] for row in rows]
            sheets.append({"name": f"Page {pno + 1} Table {tno}", "rows": clean})
    if sheets and combine:
        rows = _document_grid(doc)
        sheets = [{"name": "All tables", "rows": _combined_header(rows)}] if rows \
            else [_combine_tables(sheets)]
    return sheets


def export_tables_xlsx(doc_id, combine=False):
    """Detect tables and return them as one real .xlsx workbook -- formatted
    (bold header, borders, banded rows, auto-fit columns, frozen header)
    rather than a bare grid of values.

    combine=False gives a worksheet per table; combine=True folds them all
    into one sheet, which is what a statement running over many pages
    actually is.
    """
    doc = _doc(doc_id)
    # One grid for the whole document beats stacking grids that disagree about
    # how many columns there are; the per-page fold is kept for the documents
    # where that finds nothing.
    sheets = _sheets_for(doc, combine)
    if not sheets:
        return json.dumps({"ok": False, "reason": _no_tables_reason(doc)})
    data = _build_xlsx(sheets, creator="MiyeePDF")
    return json.dumps({
        "ok": True,
        "b64": base64.b64encode(data).decode(),
        "sheets": [{"name": s["name"], "rows": len(s["rows"]),
                    "cols": len(s["rows"][0]) if s["rows"] else 0} for s in sheets],
    })


def export_images(doc_id):
    """Pull every embedded image out of the document."""
    doc = _doc(doc_id)
    out = []
    seen = set()
    for pno in range(doc.page_count):
        for info in doc[pno].get_images(full=True):
            xref = info[0]
            if xref in seen:
                continue
            seen.add(xref)
            try:
                img = doc.extract_image(xref)
            except Exception:
                continue
            out.append({
                "name": f"image-{xref}.{img['ext']}",
                "b64": base64.b64encode(img["image"]).decode(),
                "width": img.get("width", 0),
                "height": img.get("height", 0),
            })
    return json.dumps(out)


def export_page_images(doc_id, dpi=150, fmt="png", pages_spec="", quality=90):
    """Render pages to PNG/JPEG at a chosen resolution (and JPEG quality)."""
    doc = _doc(doc_id)
    out = []
    for pno in _page_indices(doc, pages_spec):
        pix = doc[pno].get_pixmap(dpi=int(dpi))
        if fmt == "jpeg" and pix.alpha:
            pix = pymupdf.Pixmap(pix, 0)
        out.append({
            "name": f"page-{pno + 1}.{'jpg' if fmt == 'jpeg' else 'png'}",
            "b64": base64.b64encode(pix.tobytes(fmt, jpg_quality=int(quality))).decode(),
        })
    return json.dumps(out)


# --------------------------------------------------------------------------
# annotations / review
# --------------------------------------------------------------------------

def annotate(doc_id, pno, kind, x0, y0, x1, y1, text="", color="#ffd400", author=""):
    """Add a real PDF annotation that other readers (including Acrobat) understand."""
    page = _doc(doc_id)[int(pno)]
    rect = _rect_from_fracs(page, float(x0), float(y0), float(x1), float(y1))
    rgb = _hex_to_rgb(color)
    annot = None

    if kind == "highlight":
        annot = page.add_highlight_annot(rect)
    elif kind == "underline":
        annot = page.add_underline_annot(rect)
    elif kind == "strikeout":
        annot = page.add_strikeout_annot(rect)
    elif kind == "squiggly":
        annot = page.add_squiggly_annot(rect)
    elif kind == "note":
        annot = page.add_text_annot(rect.tl, text or "Note")
    elif kind == "freetext":
        annot = page.add_freetext_annot(rect, text or "", fontsize=11, text_color=(0, 0, 0))
    elif kind == "rect":
        annot = page.add_rect_annot(rect)
    else:
        raise ValueError(f"unknown annotation type '{kind}'")

    if annot is not None:
        if kind in ("highlight", "underline", "strikeout", "squiggly", "rect"):
            annot.set_colors(stroke=rgb)
        if text and kind not in ("note", "freetext"):
            annot.set_info(content=text)
        if author:
            annot.set_info(title=author)
        annot.update()
    return True


def list_annotations(doc_id):
    doc = _doc(doc_id)
    out = []
    for pno, page in enumerate(doc):
        for annot in page.annots():
            info = annot.info
            out.append({
                "page": pno,
                "type": annot.type[1],
                "content": info.get("content", ""),
                "author": info.get("title", ""),
            })
    return json.dumps(out)


def flatten_annotations(doc_id):
    """Bake annotations into the page content so they cannot be edited away."""
    doc = _doc(doc_id)
    return doc.tobytes(garbage=3, deflate=True)


# --------------------------------------------------------------------------
# compare two documents
# --------------------------------------------------------------------------

_MOVE_MIN_WORDS = 5


def _pair_moves(pages):
    """Recognise text that moved rather than being rewritten.

    A word diff reports a paragraph shifted to the next page as a deletion
    here and an unrelated insertion there, which reads as two rewrites and
    inflates the count. Where the wording is identical, it moved - and that
    is a different fact about the document, worth saying rather than hiding
    among the edits.

    Only runs of a few words are paired: a single word appearing in two
    places is a coincidence, not a move.
    """
    removed, added = [], []
    for page in pages:
        for change in page["changes"]:
            key = " ".join(change["old"].lower().split())
            if change["type"] == "delete" and len(key.split()) >= _MOVE_MIN_WORDS:
                removed.append((key, page, change))
            key = " ".join(change["new"].lower().split())
            if change["type"] == "insert" and len(key.split()) >= _MOVE_MIN_WORDS:
                added.append((key, page, change))

    taken = set()
    moves = 0
    for key, from_page, gone in removed:
        for i, (other, to_page, arrived) in enumerate(added):
            if i in taken or other != key:
                continue
            taken.add(i)
            moves += 1
            gone["type"] = "move"
            gone["movedTo"] = to_page["page"]
            gone["new"] = arrived["new"]
            arrived["type"] = "moved-here"
            arrived["movedFrom"] = from_page["page"]
            arrived["old"] = gone["old"]
            break

    # The arrival is the same fact as the departure, so it is not counted
    # twice; it stays in the list so the page it landed on shows it.
    for page in pages:
        page["changes"] = [c for c in page["changes"] if c["type"] != "moved-here"] + \
                          [c for c in page["changes"] if c["type"] == "moved-here"]
    return moves


def compare(doc_a, doc_b):
    """Word-level diff between two versions, page by page."""
    a, b = _doc(doc_a), _doc(doc_b)
    pages = []
    for pno in range(max(a.page_count, b.page_count)):
        text_a = a[pno].get_text() if pno < a.page_count else ""
        text_b = b[pno].get_text() if pno < b.page_count else ""
        words_a, words_b = text_a.split(), text_b.split()
        matcher = difflib.SequenceMatcher(None, words_a, words_b)

        changes = []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            changes.append({
                "type": tag,
                "old": " ".join(words_a[i1:i2])[:400],
                "new": " ".join(words_b[j1:j2])[:400],
            })
        pages.append({
            "page": pno,
            "similarity": round(matcher.ratio() * 100, 1),
            "changes": changes,
            "onlyIn": ("a" if pno >= b.page_count else "b" if pno >= a.page_count else None),
        })

    moves = _pair_moves(pages)
    edits = sum(1 for p in pages for c in p["changes"]
                if c["type"] not in ("move", "moved-here"))
    return json.dumps({"pages": pages, "totalChanges": edits + moves,
                       "edits": edits, "moves": moves})


def highlight_differences(doc_id, other_id):
    """Highlight, in `doc_id`, the words that differ from `other_id`."""
    doc, other = _doc(doc_id), _doc(other_id)
    marked = 0
    for pno in range(doc.page_count):
        if pno >= other.page_count:
            break
        # Hold one page object: annotations created from a throwaway `doc[pno]`
        # get garbage-collected and unbind before they can be updated.
        page = doc[pno]
        words_new = page.get_text().split()
        words_old = other[pno].get_text().split()
        matcher = difflib.SequenceMatcher(None, words_old, words_new)
        changed = set()
        for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
            if tag in ("replace", "insert"):
                changed.update(words_new[j1:j2])
        for token in list(changed)[:400]:
            token = token.strip(".,;:()[]\"'")
            if len(token) < 2:
                continue
            for rect in page.search_for(token):
                annot = page.add_highlight_annot(rect)
                annot.set_colors(stroke=(1, 0.85, 0.2))
                annot.update()
                marked += 1
    return marked


# --------------------------------------------------------------------------
# sensitive-data discovery
# --------------------------------------------------------------------------

# Ordered most-specific first. When two patterns claim overlapping text the
# earlier entry wins, which stops an IFSC code being found inside a GSTIN or a
# "phone number" being found inside a card number.
_SENSITIVE_PATTERNS = [
    ("GSTIN",    r"\b\d{2}[A-Z]{5}\d{4}[A-Z]\d[Z][A-Z\d]\b",            "GST registration number"),
    ("PAN",      r"\b[A-Z]{5}\d{4}[A-Z]\b",                             "Permanent Account Number"),
    ("IFSC",     r"\b[A-Z]{4}0[A-Z0-9]{6}\b",                           "Bank branch (IFSC) code"),
    ("Card",     r"\b(?:\d[ -]?){12,18}\d\b",                           "Payment card number"),
    ("Aadhaar",  r"\b[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}\b",                 "Aadhaar number"),
    ("Email",    r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b",                     "Email address"),
    ("Phone",    r"(?:\+91[ -]?)?\b[6-9]\d{9}\b",                       "Mobile number"),
    ("Account",  r"\b\d{9,18}\b",                                       "Bank account number"),
    ("IPAddr",   r"\b(?:\d{1,3}\.){3}\d{1,3}\b",                        "IP address"),
]

_VERHOEFF_D = (
    (0,1,2,3,4,5,6,7,8,9),(1,2,3,4,0,6,7,8,9,5),(2,3,4,0,1,7,8,9,5,6),
    (3,4,0,1,2,8,9,5,6,7),(4,0,1,2,3,9,5,6,7,8),(5,9,8,7,6,0,4,3,2,1),
    (6,5,9,8,7,1,0,4,3,2),(7,6,5,9,8,2,1,0,4,3),(8,7,6,5,9,3,2,1,0,4),
    (9,8,7,6,5,4,3,2,1,0),
)
_VERHOEFF_P = (
    (0,1,2,3,4,5,6,7,8,9),(1,5,7,6,2,8,3,0,9,4),(5,8,0,3,7,9,6,1,4,2),
    (8,9,1,6,0,4,3,5,2,7),(9,4,5,3,1,2,6,8,7,0),(4,2,8,6,5,7,3,9,0,1),
    (2,7,9,3,8,0,6,4,1,5),(7,0,4,6,9,1,3,2,5,8),
)


def _luhn_ok(digits):
    """Standard card checksum - rejects most random digit runs."""
    total, alt = 0, False
    for ch in reversed(digits):
        n = int(ch)
        if alt:
            n *= 2
            if n > 9:
                n -= 9
        total += n
        alt = not alt
    return total % 10 == 0


def _verhoeff_ok(digits):
    """Aadhaar checksum. Without it any 12-digit invoice figure looks like one."""
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _VERHOEFF_D[c][_VERHOEFF_P[i % 8][int(ch)]]
    return c == 0


def _validate_hit(kind, text):
    """Second-stage check so a matching shape alone is not enough."""
    digits = "".join(ch for ch in text if ch.isdigit())
    if kind == "Card":
        return 13 <= len(digits) <= 19 and _luhn_ok(digits)
    if kind == "Aadhaar":
        return len(digits) == 12 and _verhoeff_ok(digits)
    if kind == "IPAddr":
        return all(0 <= int(p) <= 255 for p in text.split("."))
    if kind == "Account":
        # A bare digit run is the weakest signal; keep it plausible.
        return 9 <= len(digits) <= 18
    return True


def _page_word_index(page):
    """Join a page's words into one string, remembering each word's rect.

    Working from positioned words (rather than the raw text) means a match that
    straddles a line break still maps back to the right rectangles.
    """
    words = page.get_text("words")
    parts, spans = [], []
    cursor = 0
    for w in words:
        token = w[4]
        if not token:
            continue
        start = cursor
        parts.append(token)
        cursor += len(token)
        spans.append((start, cursor, pymupdf.Rect(w[0], w[1], w[2], w[3])))
        parts.append(" ")
        cursor += 1
    return "".join(parts), spans


def scan_sensitive(doc_id, kinds_json=None):
    """Find personal and financial identifiers across the document.

    Returns one entry per occurrence with the page, the matched text and the
    rectangles covering it, ready to feed straight into redaction.
    """
    import re

    doc = _doc(doc_id)
    wanted = None
    if kinds_json:
        wanted = set(json.loads(kinds_json) if isinstance(kinds_json, str) else kinds_json)

    compiled = [(k, re.compile(p), label) for k, p, label in _SENSITIVE_PATTERNS
                if wanted is None or k in wanted]

    results = []
    for pno, page in enumerate(doc):
        text, spans = _page_word_index(page)
        if not text.strip():
            continue
        pw, ph = page.rect.width, page.rect.height

        claimed = []          # char ranges already taken by a more specific pattern
        for kind, rx, label in compiled:
            for m in rx.finditer(text):
                start, end = m.span()
                value = m.group().strip()
                if not _validate_hit(kind, value):
                    continue
                if any(start < c_end and end > c_start for c_start, c_end in claimed):
                    continue
                claimed.append((start, end))

                rects = [r for s, e, r in spans if s < end and e > start]
                if not rects:
                    continue
                results.append({
                    "page": pno,
                    "kind": kind,
                    "label": label,
                    "text": value,
                    "rects": [{"xFrac": r.x0 / pw, "yFrac": r.y0 / ph,
                               "wFrac": r.width / pw, "hFrac": r.height / ph} for r in rects],
                })

    summary = {}
    for hit in results:
        summary[hit["kind"]] = summary.get(hit["kind"], 0) + 1
    return json.dumps({"hits": results, "summary": summary, "total": len(results)})


def mask_value(kind, value):
    """A partly-hidden version of an identifier, for redaction that leaves a
    document still usable.

    Blacking out every card number in a statement makes it unreconcilable;
    "XXXXXXXXXXXX4291" leaves you able to tell one card from another while the
    number itself is gone from the file. What is kept is the part that
    identifies without disclosing, and it differs by kind: an email keeps its
    domain, an IFSC code its bank, a GSTIN its state, an address its network.
    """
    text = str(value)

    if kind == "Email":
        # The domain says who it is with; the mailbox is the personal part.
        local, _, domain = text.partition("@")
        return ("X" * max(len(local), 1)) + "@" + domain if domain else "X" * len(text)

    if kind == "IFSC":
        # First four letters are the bank; the rest is the branch.
        return text[:4] + "X" * max(len(text) - 4, 0)

    if kind == "GSTIN":
        # First two digits are the state code.
        return text[:2] + "X" * max(len(text) - 2, 0)

    if kind == "IPAddr":
        head, _, _ = text.partition(".")
        return head + ".X.X.X"

    # Everything else keeps its last four characters, the convention every
    # bank statement and receipt already uses. Separators are left in place so
    # the masked value still reads at a glance.
    keep, out = 4, []
    for ch in reversed(text):
        if not ch.isalnum():
            out.append(ch)
        elif keep > 0:
            out.append(ch)
            keep -= 1
        else:
            out.append("X")
    return "".join(reversed(out))


def redact_hits(doc_id, hits_json, fill="#000000", mode="box"):
    """Permanently remove the scanned hits the user selected.

    mode="box"  paints the area over and deletes what was under it.
    mode="mask" does the same, then writes a partly-hidden version of the
                value back in its place, so the page still reads.

    Either way the original characters are deleted from the content stream,
    not covered up: nothing is recoverable from the saved file.
    """
    doc = _doc(doc_id)
    hits = json.loads(hits_json) if isinstance(hits_json, str) else hits_json
    colour = _hex_to_rgb(fill)
    masking = str(mode) == "mask"
    touched = set()

    for hit in hits:
        page = doc[int(hit["page"])]
        rects = []
        for r in hit["rects"]:
            rects.append(_rect_from_fracs(page, float(r["xFrac"]), float(r["yFrac"]),
                                          float(r["xFrac"]) + float(r["wFrac"]),
                                          float(r["yFrac"]) + float(r["hFrac"])))
        if masking:
            # One annotation over the whole value, so the replacement text is
            # laid out once rather than broken across each word rectangle.
            box = rects[0]
            for rect in rects[1:]:
                box = box | rect
            replacement = mask_value(hit.get("kind", ""), hit.get("text", ""))
            size = max(min(box.height * 0.7, 11.0), 4.0)
            page.add_redact_annot(box, text=replacement, fontname="helv",
                                  fontsize=size, align=pymupdf.TEXT_ALIGN_LEFT,
                                  text_color=(0, 0, 0), fill=(1, 1, 1))
        else:
            for rect in rects:
                page.add_redact_annot(rect, fill=colour)
        touched.add(int(hit["page"]))

    for pno in touched:
        doc[pno].apply_redactions(images=_REDACT_IMAGES)
    return len(hits)


def scan_and_redact(doc_id, kinds_json=None, fill="#000000", mode="box"):
    """Scan a document and remove everything found, in one call.

    This is what a batch run needs: across twenty files there is nobody to
    tick boxes, so the choice of what to look for is made once and applied to
    each. Returns what was removed, per kind, so the run can be reported
    honestly rather than as a bare success.
    """
    found = json.loads(scan_sensitive(doc_id, kinds_json))
    if found["total"]:
        redact_hits(doc_id, found["hits"], fill, mode)
    return json.dumps({"total": found["total"], "summary": found["summary"]})


# --------------------------------------------------------------------------
# document inspection & sanitising
# --------------------------------------------------------------------------

def inspect(doc_id):
    """Report what a PDF carries beyond its visible pages.

    Answers the practical question "is this safe to send outside?" - author
    names, revision tooling, attachments, scripts, hidden layers and off-page
    annotations all travel with a file and are easy to forget.
    """
    doc = _doc(doc_id)
    findings = []

    def flag(level, area, detail):
        findings.append({"level": level, "area": area, "detail": detail})

    meta = {k: v for k, v in (doc.metadata or {}).items() if v}
    for key in ("author", "title", "subject", "keywords", "creator", "producer"):
        if meta.get(key):
            flag("warn" if key in ("author", "keywords", "subject") else "info",
                 f"Metadata · {key}", meta[key])

    try:
        xml = doc.get_xml_metadata()
        if xml:
            flag("warn", "XMP metadata", f"{len(xml)} characters of embedded XMP")
    except Exception:
        pass

    try:
        n = doc.embfile_count()
        if n:
            names = ", ".join(doc.embfile_names())
            flag("risk", "Embedded files", f"{n} attached file(s): {names}")
    except Exception:
        pass

    # JavaScript lives under the catalog's /Names tree.
    try:
        cat = doc.pdf_catalog()
        kind, value = doc.xref_get_key(cat, "Names/JavaScript")
        if kind and kind != "null":
            flag("risk", "JavaScript", "Document-level JavaScript is present")
    except Exception:
        pass

    try:
        ocgs = doc.get_ocgs()
        if ocgs:
            hidden = [v.get("name", "?") for v in ocgs.values() if not v.get("on", True)]
            flag("warn" if hidden else "info", "Optional layers",
                 f"{len(ocgs)} layer(s)" + (f"; hidden: {', '.join(hidden)}" if hidden else ""))
    except Exception:
        pass

    annots, links, scanned = 0, 0, []
    for pno, page in enumerate(doc):
        annots += len(list(page.annots()))
        page_links = page.get_links()
        links += len(page_links)
        for link in page_links:
            if link.get("uri"):
                flag("info", "External link", f"page {pno + 1}: {link['uri']}")
        if len(page.get_text().strip()) < 12 and page.get_images():
            scanned.append(pno + 1)

    if annots:
        flag("warn", "Annotations", f"{annots} comment/markup annotation(s) still attached")
    if scanned:
        preview = ", ".join(str(p) for p in scanned[:12])
        flag("info", "Scanned pages",
             f"{len(scanned)} page(s) have no searchable text (p{preview}"
             + ("…)" if len(scanned) > 12 else ")"))
    if doc.is_form_pdf:
        flag("warn", "Form fields", "Fillable form fields are present and may hold entered data")

    fonts, embedded = set(), 0
    for page in doc:
        for f in page.get_fonts(full=True):
            fonts.add(f[3])
            if f[1]:
                embedded += 1
    if fonts:
        flag("info", "Fonts", f"{len(fonts)} font(s): {', '.join(sorted(fonts)[:6])}")

    order = {"risk": 0, "warn": 1, "info": 2}
    findings.sort(key=lambda f: order.get(f["level"], 3))
    counts = {"risk": 0, "warn": 0, "info": 0}
    for f in findings:
        counts[f["level"]] = counts.get(f["level"], 0) + 1

    return json.dumps({
        "findings": findings,
        "counts": counts,
        "pages": doc.page_count,
        "encrypted": _WAS_ENCRYPTED.get(doc_id, False),
    })


def extract_attachments(doc_id):
    """The files carried inside a PDF, so they can be saved out.

    The inspector already says how many there are and can strip them; being
    told a document carries three attachments and having no way to read them
    is half an answer. An e-invoice's XML lives here.
    """
    doc = _doc(doc_id)
    found = []
    for index in range(doc.embfile_count()):
        try:
            info = doc.embfile_info(index)
            data = doc.embfile_get(index)
        except Exception:
            continue
        name = (info.get("filename") or info.get("name")
                or f"attachment-{index + 1}").strip() or f"attachment-{index + 1}"
        found.append({
            "name": name.replace("/", "-").replace("\\", "-"),
            "size": len(data),
            "description": info.get("description", "") or "",
            "b64": base64.b64encode(data).decode(),
        })
    return json.dumps(found)


def sanitize(doc_id, metadata=True, attachments=True, javascript=True,
             annotations=True, links=True, forms=True, hidden_text=True):
    """Strip the parts of a document that travel invisibly.

    Returns a plain-language list of what was removed so the action is auditable.
    """
    doc = _doc(doc_id)
    removed = []

    before_meta = {k: v for k, v in (doc.metadata or {}).items() if v}
    before_annots = sum(len(list(p.annots())) for p in doc)
    before_links = sum(len(p.get_links()) for p in doc)
    try:
        before_files = doc.embfile_count()
    except Exception:
        before_files = 0

    doc.scrub(
        metadata=bool(metadata),
        attached_files=bool(attachments),
        embedded_files=bool(attachments),
        javascript=bool(javascript),
        remove_links=bool(links),
        reset_fields=bool(forms),
        hidden_text=bool(hidden_text),
        clean_pages=True,
        redactions=True,
        reset_responses=bool(annotations),
        thumbnails=True,
        xml_metadata=bool(metadata),
    )

    if metadata and before_meta:
        removed.append(f"Document metadata ({', '.join(sorted(before_meta))})")
    if attachments and before_files:
        removed.append(f"{before_files} embedded file(s)")
    if javascript:
        removed.append("Any document-level JavaScript")
    if links and before_links:
        removed.append(f"{before_links} link(s)")
    if annotations and before_annots:
        removed.append(f"Responses on {before_annots} annotation(s)")
    if hidden_text:
        removed.append("Hidden (invisible-render) text")

    return json.dumps({"removed": removed})


# --------------------------------------------------------------------------
# find & replace across the document
# --------------------------------------------------------------------------

def _span_at(page, rect):
    """The text span sitting under a rectangle, for font/size/colour matching."""
    best, best_area = None, 0
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                sr = pymupdf.Rect(span["bbox"])
                inter = sr & rect
                if not inter.is_empty:
                    area = inter.get_area()
                    if area > best_area:
                        best, best_area = span, area
    return best


def find_occurrences(doc_id, needle, pages_spec=""):
    """Preview every place a phrase appears, before changing anything."""
    doc = _doc(doc_id)
    out = []
    for pno in _page_indices(doc, pages_spec):
        page = doc[pno]
        pw, ph = page.rect.width, page.rect.height
        for rect in page.search_for(needle):
            span = _span_at(page, rect)
            out.append({
                "page": pno,
                "font": (span or {}).get("font", ""),
                "size": round((span or {}).get("size", 0), 1),
                "xFrac": rect.x0 / pw, "yFrac": rect.y0 / ph,
                "wFrac": rect.width / pw, "hFrac": rect.height / ph,
            })
    return json.dumps({"count": len(out), "hits": out})


def find_replace(doc_id, needle, replacement, pages_spec=""):
    """Replace every occurrence, keeping each hit's own font, size and colour.

    The original glyphs are removed from the content stream rather than covered,
    so the replaced text cannot be recovered by selecting the page.
    """
    doc = _doc(doc_id)
    replaced = 0

    for pno in _page_indices(doc, pages_spec):
        page = doc[pno]
        hits = page.search_for(needle)
        if not hits:
            continue

        # Capture styling - and the document's own font - before redaction
        # destroys the spans that reference them.
        styled = []
        for rect in hits:
            span = _span_at(page, rect)
            name = span["font"] if span else ""
            flags = span["flags"] if span else 0
            buf, _ = _grab_embedded_font(doc, page, name, replacement)
            styled.append((
                rect, name, flags, buf,
                float(span["size"]) if span else rect.height * 0.8,
                _int_to_rgb(int(span["color"])) if span else (0, 0, 0),
            ))
            page.add_redact_annot(rect)

        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE)

        for rect, name, flags, buf, size, colour in styled:
            if replacement:
                font, measurer, _exact = _register_font(page, buf, name, flags)
                # Allow a little bleed past the old width; text rarely matches length.
                size = _fit_size(replacement, font, size, rect.width * 1.8, measurer)
                page.insert_text((rect.x0, rect.y1 - size * 0.22), replacement,
                                 fontsize=size, fontname=font, color=colour)
            replaced += 1

    return replaced


# --------------------------------------------------------------------------
# split a bundle wherever a pattern appears
# --------------------------------------------------------------------------

def _safe_name(text, fallback):
    keep = "".join(c if (c.isalnum() or c in " -_") else "-" for c in text).strip()
    keep = "-".join(keep.split())
    return (keep[:60] or fallback)


def split_by_pattern(doc_id, pattern, use_regex=False, name_from_match=True, preview=False):
    """Cut a combined document into parts wherever a marker appears.

    Built for statement and invoice bundles: each page carrying the marker
    starts a new document, and the matched text can name the file.
    """
    import re

    doc = _doc(doc_id)
    rx = re.compile(pattern, re.I) if use_regex else None

    starts = []
    for pno, page in enumerate(doc):
        text = page.get_text()
        matched = None
        if use_regex:
            m = rx.search(text)
            matched = m.group(0) if m else None
        elif pattern.lower() in text.lower():
            idx = text.lower().index(pattern.lower())
            # Take the rest of that line so an invoice number comes along too.
            matched = text[idx:idx + 60].splitlines()[0]
        if matched:
            starts.append((pno, matched.strip()))

    if not starts:
        return json.dumps({"parts": [], "message": "No page matched that pattern."})

    parts = []
    for i, (start, matched) in enumerate(starts):
        end = starts[i + 1][0] - 1 if i + 1 < len(starts) else doc.page_count - 1
        name = _safe_name(matched, f"part-{i + 1}") if name_from_match else f"part-{i + 1}"
        entry = {"name": f"{name}.pdf", "from": start + 1, "to": end + 1,
                 "pages": end - start + 1, "match": matched}
        if not preview:
            piece = pymupdf.open()
            piece.insert_pdf(doc, from_page=start, to_page=end)
            entry["b64"] = base64.b64encode(piece.tobytes(garbage=3, deflate=True)).decode()
            piece.close()
        parts.append(entry)

    return json.dumps({"parts": parts, "count": len(parts)})


# --------------------------------------------------------------------------
# paragraph-level editing and the document outline
# --------------------------------------------------------------------------

def get_blocks(doc_id, pno):
    """Paragraph-sized text blocks, for editing a whole passage at once."""
    page = _doc(doc_id)[int(pno)]
    pw, ph = page.rect.width, page.rect.height
    out = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        spans = [s for line in block["lines"] for s in line["spans"] if s["text"].strip()]
        if not spans:
            continue
        text = "".join(
            "".join(s["text"] for s in line["spans"]) + " " for line in block["lines"]
        ).strip()
        if not text:
            continue
        lead = max(spans, key=lambda s: len(s["text"]))
        x0, y0, x1, y1 = block["bbox"]
        out.append({
            "text": text,
            "font": lead["font"],
            "size": round(lead["size"], 2),
            "colorInt": lead["color"],
            "flags": lead["flags"],
            "bbox": [x0, y0, x1, y1],
            "lines": len(block["lines"]),
            "xFrac": x0 / pw, "yFrac": y0 / ph,
            "wFrac": (x1 - x0) / pw, "hFrac": (y1 - y0) / ph,
        })
    return json.dumps(out)


def edit_block(doc_id, pno, bbox, new_text, font_name="", size=0, color_int=0,
               flags=0, grow=True, cover=False):
    """Replace a whole paragraph, reflowing the replacement inside its box.

    Unlike span editing this rewraps across lines, so the new wording does not
    have to be the same length as the old. `cover` behaves as in edit_text: on
    a page read from a scan, the paper colour is painted over the region first
    so the imaged wording underneath does not show through.
    """
    doc = _doc(doc_id)
    page = doc[int(pno)]
    x0, y0, x1, y1 = [float(v) for v in bbox]
    rect = pymupdf.Rect(x0, y0, x1, y1)

    # Same as edit_text: capture the document's own font before redaction.
    buf, _ = _grab_embedded_font(doc, page, font_name, new_text)
    colour = _int_to_rgb(int(color_int))
    start_size = float(size) or 11.0

    page.add_redact_annot(rect)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE)

    if cover:
        pad = max(start_size * 0.25, 1.0)
        patch = pymupdf.Rect(rect.x0 - pad, rect.y0 - pad, rect.x1 + pad, rect.y1 + pad)
        page.draw_rect(patch, color=None, fill=_sample_background(page, patch),
                       width=0, overlay=True)

    if not new_text:
        return json.dumps({"ok": True, "size": 0, "grew": False, "exact": False,
                           "covered": bool(cover)})

    font, _measurer, exact = _register_font(page, buf, font_name, flags)

    # A paragraph may need more room than the original occupied. Try the box as
    # it is, then allow it to grow downward, and only then shrink the type.
    target = pymupdf.Rect(rect)
    grew = False
    for attempt in range(3):
        probe = pymupdf.open()
        probe_page = probe.new_page(width=page.rect.width, height=page.rect.height)
        # The reused font is registered on the real page only, so the throwaway
        # measuring page needs its own copy or the probe measures the wrong face.
        if exact and buf:
            try:
                probe_page.insert_font(fontname=font, fontbuffer=buf)
            except Exception:
                pass
        leftover = probe_page.insert_textbox(target, new_text, fontsize=start_size,
                                             fontname=font, align=0)
        probe.close()
        if leftover >= 0:
            break
        if grow and attempt == 0:
            room = page.rect.height - target.y1 - 24
            if room > 12:
                target = pymupdf.Rect(target.x0, target.y0, target.x1,
                                      min(page.rect.height - 24, target.y1 + room))
                grew = True
                continue
        start_size = max(5.0, start_size - 1.0)

    page.insert_textbox(target, new_text, fontsize=start_size, fontname=font,
                        color=colour, align=0)
    return json.dumps({"ok": True, "size": round(start_size, 1), "grew": grew,
                       "exact": exact, "covered": bool(cover)})


def get_outline(doc_id):
    """The document's bookmark tree as [[level, title, page], …]."""
    return json.dumps(_doc(doc_id).get_toc() or [])


def set_outline(doc_id, toc_json):
    """Replace the bookmark tree. Levels must start at 1 and step by one."""
    doc = _doc(doc_id)
    toc = json.loads(toc_json) if isinstance(toc_json, str) else toc_json

    cleaned, last_level = [], 0
    for row in toc:
        level = max(1, int(row[0]))
        title = str(row[1]).strip() or "Untitled"
        page = min(max(1, int(row[2])), doc.page_count)
        # PDF outlines reject a jump of more than one level at a time.
        level = min(level, last_level + 1)
        cleaned.append([level, title, page])
        last_level = level

    doc.set_toc(cleaned)
    return len(cleaned)
