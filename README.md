# MiyeePDF

**Built by Vipin Nair**

An Acrobat-class PDF suite that runs **entirely in your browser**. No file is ever uploaded - every operation happens locally on your own device.

**Live:** https://vipinnairv.github.io/miyeepdfpro/

## Features

Fifteen tools, reachable from a dashboard on the home screen.

**Edit**
- **Search** - find a phrase across every page, with the line each hit sits on shown beside it, and the hit boxed on the page when you jump to it.
- **Edit Text** - rewrite text for real: the original glyphs are removed from the content stream and the replacement is drawn in their place, not pasted over the top. The point is that the result should not look edited, so the document's own embedded font is reused when it carries the characters being typed, the replacement sits on the span's own baseline rather than a guess at it, and the size is the original's unless you change it. **A−/A+** on the editing box changes the size; **dragging its right edge** changes how much room the words have, and longer wording spreads into the empty space beside it before it wraps - it is never silently shrunk to fit, which is the usual giveaway. Rewriting a line with the text it already had renders pixel-for-pixel identical to the original.
- **Fill & Sign** - draw, type or upload a signature; place initials, text, dates and check marks; fill and flatten form fields. Undo and redo throughout. A drawn signature is a picture of one: it carries no certificate and proves nothing about who signed.
- **Verify Signature** - hand it a signed 26AS, Form 16, GST return, MCA filing or invoice and it says who signed it, when, who issued their certificate, and whether a single byte has changed since. It keeps two things apart that are usually collapsed into "invalid": whether the *document* changed, and whether the *signature* was really made with that certificate's key - a tampered file typically fails the first and passes the second. It also says when a signature covers only part of the file, meaning something was appended afterwards. All of it is worked out on the device; no certificate authority is contacted, so it cannot vouch for the certificate itself - that is your reader's trust list, and the issuer is named so you can check it.
- **Bundles that stay findable** - joining thirty annexures end to end gives you a document nobody can navigate. Drop several files into Pages and each one gets a bookmark named after it, with that file's own bookmarks kept nested underneath rather than discarded, and optionally a contents page at the front whose rows are clickable links to where each document starts.
- **Photos to PDF** - pictures of paper into one document, which is how a great many documents arrive: a phone standing in for a scanner, or what a client sent on WhatsApp. Each picture is turned the right way up from the camera's own orientation flag and scaled down before it goes in, because a twelve-megapixel photograph of one A4 sheet otherwise makes a PDF nobody can email. Drag to reorder; choose a paper size for printing or let each page take the shape of its picture.
- **Trim margins** - in Pages, cut the blank borders off scanned pages. The measurement is taken from a rendering of the page rather than from its text, so it works on a scan, where every word is part of one image. Blank pages are left alone. It sets the crop box, which is what a reader shows and a printer prints - the content outside it is still in the file, so this is tidying and **not** a way to remove anything. Redact is for that.
- **Save what is attached** - the inspector could already tell you a PDF carries three files and strip them out; now you can read them. An e-invoice's XML lives there.
- **Watermark & Stamps** - watermarks, approval stamps, page numbers, headers/footers and legal **Bates numbering**, with **undo and redo** (Ctrl+Z / Ctrl+Shift+Z) over every one of them. Takes several files at once, with Bates numbering running as one sequence across the whole bundle.
- **Find & Replace** - genuine search-and-replace inside a PDF, each replacement taking the font, size and colour of the text it replaces.

**Organise**
- **Pages** - merge, split, reorder by drag-and-drop, rotate and delete, with **undo and redo** over every one of them.
- **Auto-Split** - cut a combined bundle wherever a marker appears, so each invoice or statement becomes its own file, named from the matched text.
- **Compare** - word-level diff between two versions, with changes highlighted in a downloadable copy. Text that only **moved** is reported as a move rather than as a deletion here and an unrelated rewrite there, and counted once.
- **Compress** - recompress images and clean the file while text stays real, searchable text. Takes several files at once.

**Protect**
- **Find Sensitive Data** - scan for PAN, GSTIN, Aadhaar, IFSC, card and account numbers, emails, phones and IPs, then permanently redact them. Card and Aadhaar numbers are checksum-verified (Luhn and Verhoeff) so ordinary figures are not flagged. **Everything found is marked on the page itself**, so you see exactly what is about to be deleted and can click a mark, a row or a whole category to change your mind - redaction is irreversible, and it should not be a leap of faith. Results are grouped by kind. Removal is either a **black box** or **masking**, which deletes the original characters and writes something like `XXXXXXXXXXXX4291` back in, so a statement still reconciles. Point it at a folder of files and it scans and redacts every one.
- **Inspect & Sanitize** - reveal what travels with a PDF beyond its pages (author metadata, XMP, embedded files, JavaScript, hidden layers, annotations, links) and strip it before sharing.
- **Redact** - remove content from the file rather than covering it, with a selectable box colour. On a scan, where every word is part of an image, the covered pixels are cut out of the image data itself and the rest of the picture is kept - a black rectangle painted over a picture leaves the original underneath for anyone who extracts it. Undo and redo cover an applied redaction, separately from the button that takes back the last box you drew.
- **Password Protect** - AES-256 encryption with per-permission control, and **password removal**: unlock a file and save it without one, or hand it a folder of them and get a ZIP back. A password that opens the first file is tried on the rest, so a set of statements from the same bank is asked once rather than twenty times, and only a file it does not open asks again. This also lifts owner restrictions - the kind that let a statement open freely but block copying or printing. Password-protected files can be opened by every tool: you are asked to unlock once, and the rest of the app treats it like any other document.

**Convert**
- **OCR** - recognise scanned pages and write a real *invisible text layer* back into the PDF, so the output is genuinely searchable. Contrast is lifted before reading, which measurably helps tinted and low-contrast scans; a sideways or upside-down page can be found and turned upright first; and words the reader was unsure of are marked in amber so you can check them rather than trust them silently. The Edit tab can do this in place: open a scan there and it says so, reads the page on request, and then lets you edit it like any other document.
- **Export** - a real editable **Word .docx** (headings, bold and italic preserved), a web page, plain text, tables to a formatted Excel workbook (or CSV), embedded images, and page renders to PNG or JPEG.

  **Several PDFs can go into one workbook**, a sheet per file named after it, which is what a folder of statements from the same bank actually is. **All tables can come out in one sheet**, which is what a statement printed over thirty-six pages actually is: the columns are worked out once across the whole document rather than page by page, so pages whose grids disagree still line up, the letterhead repeated on every page is left out, a heading split over two lines is joined back into one, and every row carries the page it came from. Amounts arrive as real numbers with thousands separators, right-aligned, under a frozen header.

  Statements are the hard case for table export, and they are handled three ways. Where a document rules only its header row and nothing between the transactions - which is what most bank statements do - the rows are rebuilt from where the words sit, so a hundred transactions arrive as a hundred spreadsheet rows instead of one. Where a document draws no lines at all, the columns are found from the lanes of whitespace running down the page, which works whether a column is aligned left like a narration or right like an amount. And where the pages are **scans**, the tab says so and offers to read them on the spot rather than quietly exporting an empty file.


## Finding your way around a long document

The Edit, Fill & Sign, Watermark and Redact viewers carry a **thumbnail rail**
down the side: click any page to go to it, and the page you are on is marked.
A **page number box** sits in the toolbar for jumping straight to page 84 of
120. Thumbnails are drawn only as they scroll into the rail, so a long file
opens as fast as a short one, and a thumbnail is redrawn when you change that
page, so the rail shows the document as it is now.

## Working on several files at once

Seven of the tools accept more than one PDF: **Compress**, **Password Protect**,
**Watermark & Stamps**, **OCR**, **Find & Replace**, **Inspect & Sanitize** and
**Find Sensitive Data**.
Drop a folder's worth in, set the options once, and you get a ZIP back with
every file done. A file that cannot be read, or a locked one whose password
you decline to type, is named in the summary and skipped rather than losing
the whole run.

Bates numbering treats a batch as one bundle: the sequence carries from the
last page of one file to the first page of the next, which is what a legal
filing needs.

The other tools stay single-document on purpose. Editing text, redacting,
signing, comparing, reordering pages and auto-splitting all need you to look
at the page in front of you, so a "do this to twenty files" button there would
be dishonest.

## Not losing your work

Everything happens in memory and leaves as a download, so closing the tab
used to discard changes in silence. Any tab with unsaved changes now says so
above its Save button and marks the button itself, and the browser asks
before you leave. Saving, or stepping back to the document you opened,
clears it.

A stray Back or a refresh no longer costs you the work either. The document
you are working on is kept in the browser's own storage **on your device**
and offered back the next time the page opens, with a Restore or Discard
choice rather than silently reappearing. Nothing is uploaded to keep it, and
saving the file clears it.

## Real redaction, real encryption

Most browser PDF tools can only *draw on top of* a document. That leads to redaction that is a black rectangle over text you can still select underneath, and password-protection that flattens every page to a JPEG - destroying searchable text and bloating the file.

MiyeePDF edits the actual document instead:

| Operation | Typical browser tool | MiyeePDF |
|---|---|---|
| **Redact** | Black box drawn over text; original still recoverable | Content deleted from the file - gone from the raw bytes |
| **Protect** | Whole document re-photographed to enable encryption | AES-256 applied to the document; text stays searchable |
| **Compress** | Every page rasterized; text destroyed | Only images recompressed; text stays vector |
| **Edit text** | White box over old text, new text drawn on top | Original text replaced, matching font, size and colour |
| **Sign** | A picture of a signature, proving nothing | Optional real certificate signature, verified intact by an independent validator |
| **Mask an ID** | Characters covered; still selectable underneath | Original deleted, a masked stub written in its place |

## First load

The engine downloads once (about 28 MB, of which 17.5 MB is the PDF engine itself) in the background while the page is usable. That step's length cannot be measured from the page - Pyodide owns the fetch and does not expose it - so rather than a bar creeping to 97% on a timer, which made a stalled download look almost finished, it says how long it has been going and leaves the bar indeterminate. Past four minutes it says so, and offers to try again. A service worker then keeps it in Cache Storage rather than the ordinary HTTP cache, which is evicted far more readily, so later visits start from disk and the app keeps working with no connection at all.

## While it works

The engine runs in a Web Worker, not in the page. That is what makes the
interface stay alive during a long job: the main thread has nothing to do but
draw, so the progress bar actually moves and the browser never offers to kill
the tab. Where the engine can count pages - shrinking images, reading a page,
measuring one - the bar is that real count; where there is nothing to count it
stays indeterminate and shows how long the job has been running, rather than
inventing a percentage.

## Development

```
index.html        UI markup
app.js            UI layer + engine bootstrap (no PDF logic)
engine.worker.js  hosts Pyodide and PyMuPDF off the main thread
pdf_engine.py     every PDF operation
style.css         design tokens and components
sw.js             service worker: offline support and engine caching
vendor/           PyMuPDF WebAssembly wheel
```

Built on [PyMuPDF](https://pymupdf.readthedocs.io/) 1.28.2 compiled to WebAssembly, running under [Pyodide](https://pyodide.org/) 314.0.5, with [Tesseract.js](https://tesseract.projectnaptha.com/) for OCR recognition, [JSZip](https://stuk.github.io/jszip/) for multi-file exports and [node-forge](https://github.com/digitalbazaar/forge) for certificate signing.

The PyMuPDF wheel is committed under `vendor/` deliberately: PyPI does not send CORS headers, so the browser must load it same-origin.

**Cache busting:** `app.js` and `style.css` are loaded with a `?v=` query. When changing either, bump `APP_VERSION` in `app.js` and the matching query strings in `index.html` - otherwise browsers can serve a stale mix of old and new assets.
