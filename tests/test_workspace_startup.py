"""The local compatibility entry must never silently launch a different app/DB."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys

import pytest

from config import ROOT


WORKSPACE = ROOT.parents[2]
SHELL = shutil.which('pwsh') or shutil.which('powershell')
pytestmark = pytest.mark.skipif(
    os.name != 'nt' or not SHELL or WORKSPACE / 'data/runtime/remote-auth' != ROOT,
    reason='Windows local workspace compatibility entry only',
)


def environment():
    return {key: value for key, value in os.environ.items()
            if not key.startswith('FITNESS_') and key != 'DASHSCOPE_API_KEY'}


def launch(*args, env=None, cwd=None):
    # PowerShell 7 emits UTF-8 even when Python inherits a Windows GBK locale.
    # Keep legacy Windows PowerShell's locale encoding when pwsh is unavailable.
    encoding = 'utf-8' if Path(SHELL).stem.lower() == 'pwsh' else None
    return subprocess.run([SHELL, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
        str(WORKSPACE / 'scripts/start.ps1'), *args], cwd=cwd or WORKSPACE,
        env=env or environment(), capture_output=True, text=True, encoding=encoding, timeout=30)


def test_existing_start_entry_targets_current_source_and_original_records(tmp_path):
    result = launch('-CheckOnly', cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    info = json.loads(result.stdout)
    assert Path(info['ApplicationDirectory']).resolve() == ROOT
    assert Path(info['Database']).resolve() == WORKSPACE / 'data/runtime/fitness.sqlite3'
    assert Path(info['Python']).resolve() == WORKSPACE / '.venv/Scripts/python.exe'
    assert info['Port'] == 8765
    assert (info['RetrievalMode'], info['Tokenizer']) == ('hybrid', 'jieba')
    assert not info['CoachRequested'] and not info['KnowledgeRequested']


def test_explicit_database_and_model_preferences_remain_effective(tmp_path):
    env = environment()
    env['DASHSCOPE_API_KEY'] = 'synthetic-startup-key-never-called'
    env['FITNESS_DB_PATH'] = str(tmp_path / 'custom.sqlite3')
    result = launch('-CheckOnly', env=env)
    assert result.returncode == 0, result.stderr
    info = json.loads(result.stdout)
    assert Path(info['Database']) == tmp_path / 'custom.sqlite3'
    assert info['CoachRequested'] and info['KnowledgeRequested']
    assert env['DASHSCOPE_API_KEY'] not in result.stdout + result.stderr
    assert not (tmp_path / 'custom.sqlite3').exists()
    manual = launch('-CheckOnly', '-ManualOnly', env=env)
    assert manual.returncode == 0, manual.stderr
    info = json.loads(manual.stdout)
    assert info['ModelProvider'] == 'disabled' and not info['CoachRequested']


def test_occupied_port_does_not_silently_start_another_instance(tmp_path):
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        port = listener.getsockname()[1]
        if port > 65515:
            pytest.skip('OS selected a port outside the launcher range')
        result = launch('-Port', str(port), '-DatabasePath', str(tmp_path / 'unused.sqlite3'))
    assert result.returncode != 0
    assert 'already in use' in result.stderr
    assert not (tmp_path / 'unused.sqlite3').exists()


def test_uvicorn_app_entry_imports_current_modules_and_keeps_explicit_database(tmp_path):
    env = environment()
    env['FITNESS_DB_PATH'] = str(tmp_path / 'isolated.sqlite3')
    env['DASHSCOPE_API_KEY'] = 'synthetic-import-key-never-called'
    env['FITNESS_PHOTO_ENABLED'] = 'false'
    code = ('import app, config, json; s=app.app.state.settings; '
            'print(json.dumps({"source":str(config.ROOT), "db":str(s.database_path), '
            '"coach":s.coach_enabled, "photo":s.photo_enabled, "mode":s.retrieval_mode}))')
    result = subprocess.run([sys.executable, '-X', 'utf8', '-c', code], cwd=WORKSPACE,
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    info = json.loads(result.stdout)
    assert Path(info['source']) == ROOT and Path(info['db']) == tmp_path / 'isolated.sqlite3'
    assert info['coach'] and not info['photo'] and info['mode'] == 'hybrid'
    assert not (tmp_path / 'isolated.sqlite3').exists()
