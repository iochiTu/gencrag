"""The supplied .env example must load, including its inline comments."""
import os
import subprocess
import sys
from pathlib import Path


def test_example_env_loads_without_overriding_environment(tmp_path):
    root = Path(__file__).resolve().parents[1]
    (tmp_path / '.env').write_text((root / '.env.example').read_text())
    (tmp_path / 'config.py').write_text((root / 'app/config.py').read_text().replace(
        'ROOT = Path(__file__).resolve().parent.parent',
        'ROOT = Path(__file__).resolve().parent'))
    env = {k: v for k, v in os.environ.items() if not k.startswith(('STAFF_', 'FOUNDRY_', 'OLLAMA_'))}
    env['LLM_PROVIDER'] = 'mock'
    env['SECRET_KEY'] = 'literal-${secret}#value'
    result = subprocess.run([sys.executable, '-c',
        "from config import settings; "
        "assert settings.staff_session_days == 30; "
        "assert settings.foundry_auth == 'key'; "
        "assert settings.foundry_parse_deployment == settings.foundry_chat_deployment; "
        "assert settings.provider == 'mock'; "
        "assert settings.secret_key == 'literal-${secret}#value'"],
        cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
