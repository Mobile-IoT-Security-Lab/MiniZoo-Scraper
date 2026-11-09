"""Reject private material from the Git index and intended public working tree.

This small, offline guard complements Gitleaks. Findings contain paths and rule
names only; matched values are never printed. Ignored local runtime files are
excluded unless they have been force-added to Git.
"""
import argparse
from pathlib import Path, PurePosixPath
import re
import subprocess

PRIVATE_DIRS = {'private', 'data', 'output', 'outputs', 'reports', 'screenshots',
                'captures', 'checkpoints', 'backups', 'collection_results', 'active_runs',
                'analysis_results', '.automation_state', 'node_modules', '.venv',
                '__pycache__', '.idea', '.vscode', 'MiniZooScraper-private'}
DATA_SUFFIXES = {'.csv', '.tsv', '.jsonl', '.ndjson', '.xlsx', '.xls', '.wxapkg',
                 '.apk', '.db', '.sqlite', '.sqlite3', '.log', '.zip', '.bundle', '.tar',
                 '.gz', '.tgz', '.7z', '.pem', '.key', '.p12', '.pfx', '.keystore',
                 '.jks', '.png', '.jpg', '.jpeg', '.webp', '.mp4', '.pyc', '.conf'}
ALLOWED_DATA = {'version_scraper/examples/apps.csv', 'docs/images/mini-app-workflow.png'}
ALLOWED_JSON = {'version_scraper/config.example.json', 'version_scraper/package.json',
                'version_scraper/package-lock.json'}
PRIVATE_NAMES = {'config.json', 'config.local.json', '.npmrc', '.pypirc',
                 '.netrc', '.DS_Store', 'minizoo_core_path.txt'}
URL = re.compile(r'\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s<>"\']+')
HOME_PATH = re.compile(r'/(?:Users|home)/([^/\s<>"\']+)')
VOLUME_PATH = re.compile(r'/(?:Volumes)/([^/\r\n<>"\']+)')
PLACEHOLDERS = {'user', 'you', 'username', 'yourname', 'your_user', 'your_username',
                'your_volume', 'your_disk', 'example', 'collector', 'runner',
                'volume', 'external', 'data', '$user', '${user}'}
RULES = {
    'private-key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'),
    'credential-token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{40,}'
                                   r'|AIza[0-9A-Za-z_-]{35}|(?:AKIA|ASIA)[A-Z0-9]{16}'
                                   r'|ya29\.[A-Za-z0-9._-]{20,}|sk-(?:proj-)?[A-Za-z0-9_-]{24,})\b'),
    'oauth-refresh-token': re.compile(r'\b1//[A-Za-z0-9_-]{20,}'),
    'credential-url': re.compile(r'https?://[^\s/:"\'<>]+:[^\s/@"\'<>]+@[^\s/"\'<>]+'),
    'device-serial': re.compile(r'\b[0-9]{5}[A-Z]{4}[0-9]{5}\b'),
    'private-folder-url': re.compile(r"(?:https?://)?[A-Za-z0-9.-]+(?:/[A-Za-z0-9._~-]+)*/folders/[A-Za-z0-9_-]{20,}"),
    'literal-resource-id': re.compile(r'''["']?(?:[A-Za-z_][A-Za-z0-9_]*_)?(?:root|parent|folder)_id["']?\s*[:=]\s*["'][A-Za-z0-9_-]{20,}["']'''),
}
PERSONAL_EMAIL_DOMAINS = {'gmail.com', 'googlemail.com', 'hotmail.com', 'outlook.com',
                          'live.com', 'yahoo.com', 'icloud.com', 'proton.me', 'protonmail.com'}


def git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True)
    if result.returncode:
        raise ValueError('Git inventory failed')
    return result.stdout


def forbidden_path(name):
    path = PurePosixPath(name)
    lower = path.name.lower()
    if path.is_absolute() or '..' in path.parts or any(
            part in PRIVATE_DIRS or part.startswith('.minizoo-query-') for part in path.parts):
        return True
    if name in ALLOWED_DATA or name in ALLOWED_JSON:
        return False
    if lower in {name.casefold() for name in PRIVATE_NAMES} or lower.startswith('._'):
        return True
    if lower.startswith('.env') and lower != '.env.example':
        return True
    if lower.endswith(('.local.json', '.local.env', '.db-shm', '.db-wal', '.sqlite-wal', '.sqlite-shm')):
        return True
    return path.suffix.lower() in DATA_SUFFIXES | {'.json'}


def text_rules(raw):
    if b'\0' in raw:
        return set()
    text = raw.decode('utf-8', errors='replace')
    findings = {name for name, pattern in RULES.items() if pattern.search(text)}
    for match in re.finditer(r'\b[A-Za-z0-9_.+%-]+@([A-Za-z0-9.-]+)\b', text):
        if match[1].casefold() in PERSONAL_EMAIL_DOMAINS:
            findings.add('personal-email')
    urls = [match.span() for match in URL.finditer(text)]
    for rule, pattern in (('personal-home-path', HOME_PATH), ('personal-volume-path', VOLUME_PATH)):
        for match in pattern.finditer(text):
            if any(start <= match.start() < end for start, end in urls):
                continue
            if match[1].strip().casefold() not in PLACEHOLDERS:
                findings.add(rule)
    return findings


def audit(root):
    root = Path(root).resolve()
    findings = set()
    staged = {}
    for entry in git(root, 'ls-files', '--stage', '-z').split(b'\0'):
        if entry:
            header, raw_name = entry.split(b'\t', 1)
            mode, oid, _stage = header.decode().split()
            staged[raw_name.decode('utf-8', errors='surrogateescape')] = (mode, oid)
    others = git(root, 'ls-files', '--others', '--exclude-standard', '-z').split(b'\0')
    names = set(staged) | {name.decode('utf-8', errors='surrogateescape') for name in others if name}
    for name in sorted(names):
        mode, oid = staged.get(name, (None, None))
        if forbidden_path(name):
            findings.add((name, 'private-file'))
        if mode == '120000':
            findings.add((name, 'symlink'))
            continue
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            findings.add((name, 'symlink-or-outside-repository'))
            continue
        if mode == '160000':
            continue  # The pinned external repository is audited separately.
        if oid:
            findings.update((name, 'index-' + rule) for rule in text_rules(git(root, 'cat-file', 'blob', oid)))
        if path.is_file():
            findings.update((name, rule) for rule in text_rules(path.read_bytes()))
    for line in git(root, 'log', '--all', '--format=%H%x00%ae%x00%ce').splitlines():
        commit, author, committer = line.decode().split('\0')
        if any(email.rsplit('@', 1)[-1].casefold() in PERSONAL_EMAIL_DOMAINS for email in (author, committer)):
            findings.add(('<history>/' + commit[:12], 'personal-email-in-commit'))
    return sorted(findings)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    try:
        findings = audit(args.repo)
    except (OSError, ValueError):
        print('Publication check could not inspect the repository.')
        return 2
    for path, rule in findings:
        print(f'{ascii(path)}: {rule}')
    if findings:
        print(f'Publication check failed: {len(findings)} finding(s).')
        return 1
    print('Publication check passed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
