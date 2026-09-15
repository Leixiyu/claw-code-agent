"""Training contract: user task indexes and live dataset/model metadata."""
from __future__ import annotations
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import httpx

from src import business_functions as business
from src.agent_tools import default_tool_registry
from src.user_workspace import add_task_id, read_task_ids, read_tasks


class ModelTrainingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.kwargs = {'workspace_root': self.workspace, 'timeout_seconds': 2}
        self.arguments = {'scenario': 'fire_inspection', 'dataset_ref': 'd1', 'idempotency_key': 'test-training'}
        self.manifest = {'dataset_id': 'd1', 'scenario': 'fire_inspection', 'status': 'ready'}
        self.metadata = {'model_id': 'm1', 'dataset_id': 'd1', 'status': 'ready'}
        self.status = 'running'
        self.result_code = 200
        self.submit_code = 200
        self.calls = []
        add_task_id(self.workspace, 'processing', 'p1')
        add_task_id(self.workspace, 'training', 't1')
        def backend(request):
            self.calls.append(request)
            if request.url.host == 'processing.test':
                return httpx.Response(200, json={'manifest': self.manifest})
            if request.method == 'POST':
                return httpx.Response(self.submit_code, json={'task_id': 't1'})
            if '/status/' in request.url.path:
                return httpx.Response(200, json={'status': self.status})
            return httpx.Response(self.result_code, json={'metadata': self.metadata})
        client = httpx.Client
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'MODEL_TRAINING_API': 'training.test', 'VIDEO_PROCESSING_API': 'processing.test'}).start()
        patch.object(business.httpx, 'Client', side_effect=lambda **kwargs: client(transport=httpx.MockTransport(backend), **kwargs)).start()

    def test_schemas_and_list_registration(self):
        registry = default_tool_registry()
        for action in ('submit_model_training', 'get_model_training_status', 'get_model_training_result', 'list_model_training_tasks'):
            self.assertIn(action, registry)
        for action in ('get_model_training_status', 'get_model_training_result'):
            self.assertEqual(registry[action].parameters['required'], ['task_id'])

    def test_submit_json_and_index_without_result_files(self):
        payload = json.loads(business.submit_model_training(self.arguments, **self.kwargs)[0])
        self.assertEqual(payload['task_id'], 't1')
        posts = [request for request in self.calls if request.method == 'POST']
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].url.path, '/train')
        self.assertEqual(json.loads(posts[0].content), {'scenario': 'fire_inspection', 'dataset_ref': 'd1'})
        self.assertEqual(read_task_ids(self.workspace, 'training'), ['t1'])
        self.assertFalse((self.workspace / 'tasks').exists())

    def test_replay_without_second_post(self):
        business.submit_model_training(self.arguments, **self.kwargs)
        payload = json.loads(business.submit_model_training(self.arguments, **self.kwargs)[0])
        self.assertTrue(payload['idempotency_replayed'])
        self.assertEqual(len([r for r in self.calls if r.method == 'POST']), 1)

    def test_dataset_live_not_ready_blocks_submission(self):
        self.manifest['status'] = 'processing'
        with self.assertRaisesRegex(business.ModelTrainingError, 'not ready'):
            business.submit_model_training(self.arguments, **self.kwargs)
        self.assertFalse(any(r.method == 'POST' for r in self.calls))

    def test_dataset_identity_and_scenario_checked_live(self):
        for change, message in [({'dataset_id': 'other'}, 'not found'), ({'scenario': 'other'}, 'scenario')]:
            with self.subTest(change=change):
                self.manifest = {'dataset_id': 'd1', 'scenario': 'fire_inspection', 'status': 'ready', **change}
                with self.assertRaisesRegex(business.ModelTrainingError, message):
                    business.submit_model_training(self.arguments, **self.kwargs)
        self.assertFalse(any(r.method == 'POST' for r in self.calls))

    def test_processing_result_failure_blocks_training(self):
        self.manifest = None
        with self.assertRaisesRegex(business.ModelTrainingError, 'could not be verified'):
            business.submit_model_training(self.arguments, **self.kwargs)
        self.assertFalse(any(r.method == 'POST' for r in self.calls))

    def test_key_conflict_is_rejected(self):
        business.submit_model_training(self.arguments, **self.kwargs)
        self.manifest['dataset_id'] = 'd2'
        with self.assertRaisesRegex(business.ModelTrainingError, 'different'):
            business.submit_model_training({**self.arguments, 'dataset_ref': 'd2'}, **self.kwargs)

    def test_submit_api_errors_and_uncertain_outcome_not_retried(self):
        self.submit_code = 500
        with self.assertRaisesRegex(business.ModelTrainingError, 'HTTP 500'):
            business.submit_model_training(self.arguments, **self.kwargs)
        with self.assertRaisesRegex(business.ModelTrainingError, 'outcome is unknown'):
            business.submit_model_training(self.arguments, **self.kwargs)
        self.assertEqual(len([r for r in self.calls if r.method == 'POST']), 1)

    def test_statuses_match_backend_and_update_index(self):
        for status in ('pending', 'running', 'done', 'failed'):
            self.status = status
            payload = json.loads(business.get_model_training_status({'task_id': 't1'}, **self.kwargs)[0])
            self.assertEqual(payload, {'task_id': 't1', 'status': status,
                                      'is_terminal': status in {'done', 'failed'}, 'result_ready': status == 'done'})
            self.assertEqual(read_tasks(self.workspace, 'training')[0]['status'], status)
        self.assertFalse((self.workspace / 'tasks').exists())

    def test_status_unknown_and_missing_api(self):
        self.status = 'unknown'
        with self.assertRaisesRegex(business.ModelTrainingError, 'unsupported status'):
            business.get_model_training_status({'task_id': 't1'}, **self.kwargs)
        with patch.dict(os.environ, {'MODEL_TRAINING_API': ''}):
            with self.assertRaises(business.ModelTrainingError):
                business.get_model_training_status({'task_id': 't1'}, **self.kwargs)

    def test_result_returns_metadata_without_local_file(self):
        payload = json.loads(business.get_model_training_result({'task_id': 't1'}, **self.kwargs)[0])
        self.assertEqual(payload, {'task_id': 't1', 'status': 'done', 'model_id': 'm1', 'metadata': self.metadata})
        self.assertFalse((self.workspace / 'models').exists())
        self.assertFalse((self.workspace / 'tasks').exists())

    def test_invalid_metadata(self):
        for metadata in (None, {}, [], {'model_id': ''}):
            self.metadata = metadata
            with self.subTest(metadata=metadata), self.assertRaises(business.ModelTrainingError):
                business.get_model_training_result({'task_id': 't1'}, **self.kwargs)

    def test_model_id_is_opaque_and_not_a_filename(self):
        self.metadata['model_id'] = '../model'
        result = json.loads(business.get_model_training_result({'task_id': 't1'}, **self.kwargs)[0])
        self.assertEqual(result['model_id'], '../model')
        self.assertFalse((self.workspace / 'models').exists())

    def test_result_not_ready_and_http_errors(self):
        for code in (202, 404, 500):
            self.result_code = code
            with self.subTest(code=code), self.assertRaisesRegex(business.ModelTrainingError, f'HTTP {code}'):
                business.get_model_training_result({'task_id': 't1'}, **self.kwargs)

    def test_unowned_task_is_rejected_before_http(self):
        with self.assertRaisesRegex(business.ModelTrainingError, 'current user'):
            business.get_model_training_status({'task_id': 'someone-else'}, **self.kwargs)
        with self.assertRaisesRegex(business.ModelTrainingError, 'current user'):
            business.get_model_training_result({'task_id': 'someone-else'}, **self.kwargs)
        self.assertEqual(self.calls, [])

    def test_invalid_arguments(self):
        for args in ({}, {'scenario': 'unknown', 'dataset_ref': 'd1'}, {'scenario': 'fire_inspection'}):
            with self.subTest(args=args), self.assertRaises(business.ModelTrainingError):
                business.submit_model_training(args, **self.kwargs)
