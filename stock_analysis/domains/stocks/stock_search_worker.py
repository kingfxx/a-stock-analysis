"""Short-lived, offline pinyin converter. Never import this in the server."""
import json
import sys


def main():
    names = json.loads(sys.stdin.buffer.read(1048577).decode('utf-8'))
    if (not isinstance(names, list) or len(names) > 2000
            or any(not isinstance(name, str) or len(name) > 1000 for name in names)):
        raise ValueError('Invalid stock search input')
    from pypinyin import lazy_pinyin, Style
    rows = [[''.join(lazy_pinyin(name)), ''.join(lazy_pinyin(name, style=Style.FIRST_LETTER))]
            for name in names]
    sys.stdout.buffer.write(json.dumps(rows, ensure_ascii=False).encode('utf-8'))


if __name__ == '__main__':
    main()
