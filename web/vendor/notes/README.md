# Notes editor assets

Locally served, pinned browser distributions. No runtime CDN requests or npm install scripts.

| Library | Version | Upstream | License file |
|---|---|---|---|
| CodeMirror 5 | 5.65.21 | https://github.com/codemirror/codemirror5 | LICENSE-codemirror |
| markdown-it | 15.0.3 | https://github.com/markdown-it/markdown-it | LICENSE-markdown-it |
| markdown-it-footnote | 4.0.0 | https://github.com/markdown-it/markdown-it-footnote | LICENSE-footnote |
| DOMPurify | 3.4.17 | https://github.com/cure53/DOMPurify | LICENSE-purify |

CodeMirror and the footnote bundle were downloaded from pinned npm packages through jsDelivr. markdown-it and DOMPurify were extracted from their exact official npm release tarballs. Only the named browser assets and license texts were copied; package scripts were not executed.

`manifest.json` records the distribution SHA256 hashes. Keep library versions, licenses, browser compatibility tests and manifest in sync when updating. CodeMirror 5 is used for its standalone browser interface; CodeMirror 6 is a different integration API.
