"""Cached task states: compatibility, concurrent updates and result-only lists."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from src import business_functions as business
from src.user_workspace import (
    TASK_INDEX_FILES, add_task_id, atomic_json, read_tasks, read_task_ids,
    update_task_status,
)


class TaskIndexTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.kwargs = {'workspace_root': self.root, 'timeout_seconds': 2}

    def test_legacy_ids_are_unknown_and_lazily_upgraded(self):
        for module, filename in TASK_INDEX_FILES.items():
            with self.subTest(module=module):
                atomic_json(self.root / filename, {'task_ids': ['old', 'old', 'other']})
                self.assertEqual(read_task_ids(self.root, module), ['old', 'other'])
                self.assertEqual(read_tasks(self.root, module)[0]['status'], None)
                update_task_status(self.root, module, 'old', 'done')
                add_task_id(self.root, module, 'new')
                add_task_id(self.root, module, 'old')
                self.assertEqual(json.loads((self.root / filename).read_text()), {'tasks': [
                    {'task_id': 'old', 'status': 'done'},
                    {'task_id': 'other', 'status': None},
                    {'task_id': 'new', 'status': 'pending'},
                ]})

    def test_concurrent_updates_and_submissions_preserve_all_entries(self):
        for i in range(20):
            add_task_id(self.root, 'analysis', str(i))
        def change(i):
            if i % 2:
                update_task_status(self.root, 'analysis', str(i // 2), 'done')
            else:
                add_task_id(self.root, 'analysis', f'new-{i}')
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(change, range(40)))
        entries = read_tasks(self.root, 'analysis')
        self.assertEqual(len(entries), 40)
        self.assertEqual(sum(task['status'] == 'done' for task in entries), 20)

    def test_invalid_or_unowned_status_update_does_not_change_index(self):
        add_task_id(self.root, 'analysis', 'a1')
        before = (self.root / TASK_INDEX_FILES['analysis']).read_bytes()
        for task_id, status in (('other', 'done'), ('a1', 'unknown'), ('a1', None)):
            with self.assertRaises(ValueError):
                update_task_status(self.root, 'analysis', task_id, status)
        self.assertEqual((self.root / TASK_INDEX_FILES['analysis']).read_bytes(), before)

    def test_malformed_indexes_are_rejected(self):
        for payload in ({'tasks': None}, {'tasks': ['bad']},
                        {'tasks': [{'task_id': 'a'}]},
                        {'tasks': [{'task_id': 'a', 'status': []}]},
                        {'task_ids': [None]}):
            with self.subTest(payload=payload):
                atomic_json(self.root / TASK_INDEX_FILES['analysis'], payload)
                with self.assertRaises(ValueError):
                    read_tasks(self.root, 'analysis')

    def test_all_lists_only_query_done_results_without_writing_results(self):
        for module, name in (('analysis', 'video_analysis'), ('processing', 'video_processing'), ('training', 'model_training')):
            with self.subTest(module=module):
                for task_id, status in (('done', 'done'), ('pending', 'pending'), ('running', 'running'), ('failed', 'failed'), ('legacy', None)):
                    add_task_id(self.root, module, task_id, status=status)
                index = self.root / TASK_INDEX_FILES[module]
                before = index.read_bytes()
                with patch.object(business, f'get_{name}_status') as query_status, patch.object(
                    business, f'get_{name}_result', return_value=('{"status":"done"}', {})
                ) as query_result:
                    result = json.loads(getattr(business, f'list_{name}_tasks')({}, **self.kwargs)[0])
                query_status.assert_not_called()
                query_result.assert_called_once_with({'task_id': 'done'}, **self.kwargs)
                self.assertEqual(result['task_count'], 5)
                self.assertEqual(sum('result' in task for task in result['tasks']), 1)
                self.assertEqual(result['tasks'][-1]['status'], None)
                self.assertEqual(index.read_bytes(), before)

    def test_status_apis_update_indexes_and_preserve_cache_on_errors(self):
        client = httpx.Client
        for module, name, api in (
            ('analysis', 'video_analysis', 'VIDEO_ANALYSIS_API'),
            ('processing', 'video_processing', 'VIDEO_PROCESSING_API'),
            ('training', 'model_training', 'MODEL_TRAINING_API'),
        ):
            add_task_id(self.root, module, 'task')
            query = getattr(business, f'get_{name}_status')
            for status in ('pending', 'running', 'done', 'failed'):
                response = httpx.Response(200, json={'status': status})
                transport = httpx.MockTransport(lambda request: response)
                with self.subTest(module=module, status=status), patch.dict(os.environ, {api: 'http://business.test'}), patch.object(
                    business.httpx, 'Client', side_effect=lambda **kw: client(transport=transport, **kw)
                ):
                    payload = json.loads(query({'task_id': 'task'}, **self.kwargs)[0])
                    self.assertEqual(payload['status'], status)
                    self.assertEqual(read_tasks(self.root, module)[0]['status'], status)
            for response in (httpx.Response(500), httpx.Response(200, json={'status': 'invalid'}),
                             httpx.Response(200, content=b'not-json')):
                transport = httpx.MockTransport(lambda request: response)
                with self.subTest(module=module, response=response), patch.dict(os.environ, {api: 'http://business.test'}), patch.object(
                    business.httpx, 'Client', side_effect=lambda **kw: client(transport=transport, **kw)
                ):
                    with self.assertRaises(RuntimeError):
                        query({'task_id': 'task'}, **self.kwargs)
                    self.assertEqual(read_tasks(self.root, module)[0]['status'], 'failed')
            with patch.dict(os.environ, {api: 'business.test'}), patch.object(
                business, '_get_backend_status', return_value='done'
            ), patch.object(business, 'update_task_status', side_effect=OSError('disk full')):
                with self.assertRaisesRegex(RuntimeError, 'failed to update task status index'):
                    query({'task_id': 'task'}, **self.kwargs)


if __name__ == '__main__':
    unittest.main()
