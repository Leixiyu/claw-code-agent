from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.env_loader import load_project_env


class EnvLoaderTests(unittest.TestCase):
    def test_loads_values_and_expands_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / '.env'
            env_path.write_text(
                '\n'.join(
                    [
                        'DASHSCOPE_API_KEY="secret-value"',
                        'OPENAI_API_KEY="${DASHSCOPE_API_KEY}"',
                        'OPENAI_MODEL="qwen3-coder-next"',
                    ]
                ),
                encoding='utf-8',
            )
            with patch.dict(os.environ, {}, clear=True):
                loaded = load_project_env(env_path)
                self.assertEqual(loaded, env_path.resolve())
                self.assertEqual(os.environ['OPENAI_API_KEY'], 'secret-value')
                self.assertEqual(os.environ['OPENAI_MODEL'], 'qwen3-coder-next')

    def test_process_environment_takes_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / '.env'
            env_path.write_text('OPENAI_MODEL="from-file"\n', encoding='utf-8')
            with patch.dict(
                os.environ,
                {'OPENAI_MODEL': 'from-process'},
                clear=True,
            ):
                load_project_env(env_path)
                self.assertEqual(os.environ['OPENAI_MODEL'], 'from-process')

    def test_dashscope_key_is_used_when_openai_key_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / '.env'
            env_path.write_text('DASHSCOPE_API_KEY="secret-value"\n', encoding='utf-8')
            with patch.dict(os.environ, {}, clear=True):
                load_project_env(env_path)
                self.assertEqual(os.environ['OPENAI_API_KEY'], 'secret-value')


if __name__ == '__main__':
    unittest.main()
