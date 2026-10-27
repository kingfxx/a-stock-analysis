"""Logical fact counters; metadata-only UPSERTs and version copies are not new data."""


def counts(returned):
    return dict(inserted=0, revised=0, unchanged=0, returned=returned)


def observe(result, previous, candidate):
    result['inserted' if previous is None else 'unchanged' if previous == candidate else 'revised'] += 1


def published(result):
    return {**result, 'published': result['inserted'] + result['revised'] + result.get('enriched', 0)}
