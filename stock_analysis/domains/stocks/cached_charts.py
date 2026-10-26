"""Local-only chart reads with one explicitly pinned price generation."""
from pathlib import Path


def read(data, code, section, version=None):
    if Path(data.settings.DATABASE_PATH).exists():
        if version is None:
            version = data.shared_projection(code)[0] or 0
        bundle = data.price_bundle(code, version=version)
        if section == 'prices':
            return bundle
        result = bundle['financial' if section == 'dividends' else section]
        return {**result, 'price_version': bundle['price_version']}
    _, payload, _ = data.page_payload(code, False)
    if section == 'prices':
        return dict(price_version=0, financial=payload, valuation=payload.get('valuation', {}),
                    shareholders=payload.get('shareholders', {}), financing=payload.get('financing', {}), warnings=[])
    return payload if section in ('financial','dividends') else payload.get(section, {})
