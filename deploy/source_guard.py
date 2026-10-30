"""Explicit source restriction for the public distribution's updater."""
from pathlib import Path
import re
import subprocess

from deploy.config import ExecutionError

# 白名单：owner/repo。大小写不敏感；添加新仓库只需在这里加一行。
OFFICIAL_REPOSITORIES = (
    'xylolit-mu/OnmyojiAutoScript',
    'wentao-za/OnmyojiAutoScript',
)
_OFFICIAL_REPOSITORY_KEYS = {repo.lower() for repo in OFFICIAL_REPOSITORIES}

# 如果其他模块还在 import 这个常量，保留它作为“主仓库”即可。
OFFICIAL_REPOSITORY = 'https://github.com/xylolit-mu/OnmyojiAutoScript.git'

_URL_RE = re.compile(
    r'(?:https://github[.]com/|git@github[.]com:|ssh://git@github[.]com/)'
    r'(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:[.]git)?/?',
    re.IGNORECASE,
)


def _repository_key(value):
    """把官方支持的 URL 写法归一化成 'owner/repo' 小写形式；不合法返回 None。"""
    match = _URL_RE.fullmatch(str(value or '').strip())
    if not match:
        return None
    return f'{match.group("owner")}/{match.group("repo")}'.lower()


def is_official_repository(value):
    return _repository_key(value) in _OFFICIAL_REPOSITORY_KEYS


def validate_update_source(git, root, repository, source='origin'):
    allowed = ', '.join(
        f'https://github.com/{repo}.git' for repo in OFFICIAL_REPOSITORIES
    )
    message = ('Repository source rejected: updates require one of: '
               + allowed
               + '. No fetch or pull was performed.')
    if not is_official_repository(repository):
        raise ExecutionError(message)
    if not re.fullmatch(r'[A-Za-z0-9_-]+', source):
        raise ExecutionError(message)
    if not (Path(root) / '.git').exists():
        return  # First installation; validate its configured URL above.
    try:
        result = subprocess.run(
            [git, 'remote', 'get-url', '--all', source], cwd=root,
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ExecutionError(message) from error
    urls = result.stdout.splitlines()
    if result.returncode or not urls or not all(is_official_repository(u) for u in urls):
        raise ExecutionError(message)

