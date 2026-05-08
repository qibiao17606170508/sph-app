import json
import os
import shutil
from copy import deepcopy
from datetime import datetime

_BASE_DIR = os.environ.get('APP_BASE_DIR', os.path.dirname(os.path.abspath(__file__)))
ACCOUNTS_PATH = os.path.join(_BASE_DIR, 'accounts.json')
BASE_PROFILE_DIR = os.path.join(_BASE_DIR, 'browser-profiles', 'default')
PRIMARY_ACCOUNT_NAME = 'default'

DEFAULT_ACCOUNTS = [
    {'name': PRIMARY_ACCOUNT_NAME, 'profileDir': BASE_PROFILE_DIR, 'label': '主账号', 'status': 'needs-login', 'lastLogin': None, 'createdAt': None},
]


def _normalize_profile_dir(source_profile_dir):
    """Keep the single-account profile inside the current app base dir.

    Older versions may persist an absolute path that points to a previous
    checkout/download location, which can leave Chromium locking a stale
    directory forever. We migrate that profile into the current workspace.
    """
    target = BASE_PROFILE_DIR
    incoming = (source_profile_dir or '').strip()
    if not incoming or os.path.abspath(incoming) == os.path.abspath(target):
        return target

    os.makedirs(os.path.dirname(target), exist_ok=True)
    if os.path.isdir(incoming) and not os.path.exists(target):
        try:
            shutil.copytree(incoming, target, dirs_exist_ok=True)
        except OSError:
            pass
    return target


def _normalize_single_account(accounts):
    items = accounts if isinstance(accounts, list) else []
    source = None
    for a in items:
        if isinstance(a, dict) and a.get('name') == PRIMARY_ACCOUNT_NAME:
            source = a
            break
    if source is None:
        for a in items:
            if isinstance(a, dict):
                source = a
                break

    base = deepcopy(DEFAULT_ACCOUNTS[0])
    if source:
        base.update({
            'label': source.get('label') or base['label'],
            'status': source.get('status') or 'needs-login',
            'lastLogin': source.get('lastLogin'),
            'createdAt': source.get('createdAt'),
            'profileDir': _normalize_profile_dir(source.get('profileDir')),
        })
    base['name'] = PRIMARY_ACCOUNT_NAME
    return [base]


def loadAccounts():
    if not os.path.exists(ACCOUNTS_PATH):
        return deepcopy(DEFAULT_ACCOUNTS)
    try:
        with open(ACCOUNTS_PATH, 'r', encoding='utf-8') as f:
            accounts = json.load(f)
            normalized = _normalize_single_account(accounts)
            if accounts != normalized:
                saveAccounts(normalized)
            return normalized
    except (json.JSONDecodeError, OSError):
        bak = ACCOUNTS_PATH + '.bak'
        if os.path.exists(bak):
            try:
                with open(bak, 'r', encoding='utf-8') as f:
                    restored = json.load(f)
                if restored:
                    normalized = _normalize_single_account(restored)
                    saveAccounts(normalized)
                    return normalized
            except (json.JSONDecodeError, OSError):
                pass
        return deepcopy(DEFAULT_ACCOUNTS)


def saveAccounts(accounts):
    accounts = _normalize_single_account(accounts)
    if os.path.exists(ACCOUNTS_PATH):
        try:
            shutil.copy2(ACCOUNTS_PATH, ACCOUNTS_PATH + '.bak')
        except OSError:
            pass
    with open(ACCOUNTS_PATH, 'w', encoding='utf-8') as f:
        json.dump(accounts, f, indent=2, ensure_ascii=False)


def getAccount(name):
    if name != PRIMARY_ACCOUNT_NAME:
        return None
    accounts = loadAccounts()
    for a in accounts:
        if a['name'] == name:
            return a
    return None


def createAccount(name, label):
    raise Exception('当前为单账号模式，不能新增账号')


def deleteAccount(name):
    raise Exception('当前为单账号模式，不能删除主账号')


def updateAccount(name, updates):
    if name != PRIMARY_ACCOUNT_NAME:
        raise Exception('账号不存在：' + name)
    accounts = loadAccounts()
    for a in accounts:
        if a['name'] == name:
            allowed = {}
            if isinstance(updates, dict) and 'label' in updates:
                allowed['label'] = (updates.get('label') or '').strip() or a.get('label') or '主账号'
            a.update(allowed)
            saveAccounts(accounts)
            return a
    raise Exception('账号不存在：' + name)


def updateAccountStatus(name, status):
    if name != PRIMARY_ACCOUNT_NAME:
        return
    accounts = loadAccounts()
    for a in accounts:
        if a['name'] == name:
            a['status'] = status
            if status == 'ready':
                a['lastLogin'] = datetime.now().isoformat()
            saveAccounts(accounts)
            return

# snake_case aliases
load_accounts = loadAccounts
save_accounts = saveAccounts
get_account = getAccount
create_account = createAccount
delete_account = deleteAccount
update_account = updateAccount
update_account_status = updateAccountStatus
