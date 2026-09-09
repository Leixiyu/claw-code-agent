from __future__ import annotations

import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from src.agent_tools import build_tool_context, default_tool_registry, execute_tool
from src.agent_types import AgentRuntimeConfig
from tests.test_model_training import _write_dataset_manifest


CASES = (
    ('submit_video_analysis', '_post_uploaded_video', 'video_analysis_idempotency.json',
     {'scenario': 'fire_inspection', 'video_ref': {'type': 'upload_file', 'path': 'demo.mp4'}}),
    ('submit_video_processing', '_post_uploaded_videos', 'video_processing_idempotency.json',
     {'scenario': 'fire_inspection', 'raw_video_refs': ['demo.mp4']}),
    ('submit_model_training', '_post_model_training', 'model_training_idempotency.json',
     {'scenario': 'fire_inspection', 'dataset_ref': 'fire-inspect-01'}),
)


class BusinessIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'demo.mp4').write_bytes(b'video')
        _write_dataset_manifest(self.root)
        self.registry = default_tool_registry()
        self.context = replace(
            build_tool_context(AgentRuntimeConfig(cwd=self.root)),
            business_session_id='session-one',
        )
        env = patch.dict(os.environ, {
            'VIDEO_ANALYSIS_API': 'http://analysis.test',
            'VIDEO_PROCESSING_API': 'http://processing.test',
            'MODEL_TRAINING_API': 'http://training.test',
        })
        env.start()
        self.addCleanup(env.stop)

    def call(self, tool, args, context=None):
        return execute_tool(self.registry, tool, args, context or self.context)

    def payload(self, result):
        self.assertTrue(result.ok, result.content)
        return json.loads(result.content)

    def test_all_modules_generate_persist_and_reuse_keys_after_context_recreation(self):
        for tool, sender, filename, args in CASES:
            with self.subTest(tool=tool), patch(
                'src.business_functions.' + sender, return_value=tool + '-task'
            ) as post:
                first = self.payload(self.call(tool, args))
                restored_context = replace(
                    build_tool_context(AgentRuntimeConfig(cwd=self.root)),
                    business_session_id='session-one',
                )
                second = self.payload(self.call(tool, args, restored_context))
                self.assertEqual(first['task_id'], second['task_id'])
                self.assertEqual(first['idempotency_key'], second['idempotency_key'])
                self.assertTrue(second['idempotency_replayed'])
                self.assertEqual(post.call_count, 1)
                state = json.loads((self.root / '.port_sessions/business_functions' / filename).read_text())
                self.assertEqual(state['entries'][first['idempotency_key']]['state'], 'submitted')
                if tool == 'submit_model_training':
                    self.assertEqual(post.call_args.args[1], {
                        'scenario': 'fire_inspection', 'dataset_ref': 'fire-inspect-01',
                    })

    def test_sessions_and_endpoints_are_independent(self):
        for tool, sender, _, args in CASES:
            with self.subTest(tool=tool), patch(
                'src.business_functions.' + sender, side_effect=['first', 'second', 'third']
            ) as post:
                first = self.payload(self.call(tool, args))
                second = self.payload(self.call(tool, args, replace(self.context, business_session_id='session-two')))
                with patch.dict(os.environ, {
                    'VIDEO_ANALYSIS_API': 'http://new.test',
                    'VIDEO_PROCESSING_API': 'http://new.test',
                    'MODEL_TRAINING_API': 'http://new.test',
                }):
                    third = self.payload(self.call(tool, args))
                self.assertEqual(len({r['idempotency_key'] for r in [first, second, third]}), 3)
                self.assertEqual(post.call_count, 3)

    def test_explicit_repeat_is_itself_idempotent_and_can_be_repeated_again(self):
        for tool, sender, _, args in CASES:
            with self.subTest(tool=tool), patch(
                'src.business_functions.' + sender, side_effect=['initial', 'repeat', 'again']
            ) as post:
                first = self.payload(self.call(tool, args))
                repeat_args = dict(args, repeat_of_task_id=first['task_id'])
                second = self.payload(self.call(tool, repeat_args))
                retry = self.payload(self.call(tool, repeat_args))
                ordinary_retry = self.payload(self.call(tool, args))
                third = self.payload(self.call(tool, dict(args, repeat_of_task_id=second['task_id'])))
                self.assertEqual(second['task_id'], retry['task_id'])
                self.assertEqual(second['task_id'], ordinary_retry['task_id'])
                self.assertEqual(len({r['idempotency_key'] for r in [first, second, third]}), 3)
                self.assertEqual(post.call_count, 3)

    def test_unknown_or_other_session_repeat_is_rejected(self):
        for tool, sender, _, args in CASES:
            with self.subTest(tool=tool), patch(
                'src.business_functions.' + sender, return_value='known-task'
            ) as post:
                self.payload(self.call(tool, args))
                for parent, context in [('invented-task', self.context),
                                        ('known-task', replace(self.context, business_session_id='other'))]:
                    result = self.call(tool, dict(args, repeat_of_task_id=parent), context)
                    self.assertFalse(result.ok)
                    self.assertIn('known submission', result.content)
                self.assertEqual(post.call_count, 1)

    def test_interrupted_submission_is_persisted_before_send_and_not_sent_again(self):
        for tool, sender, filename, args in CASES:
            path = self.root / '.port_sessions/business_functions' / filename
            def interrupt(*unused):
                state = json.loads(path.read_text())
                self.assertEqual(len(state['entries']), 1)
                self.assertEqual(next(iter(state['entries'].values()))['state'], 'submitting')
                raise KeyboardInterrupt()
            with self.subTest(tool=tool), patch('src.business_functions.' + sender, side_effect=interrupt) as post:
                with self.assertRaises(KeyboardInterrupt):
                    self.call(tool, args)
                restored = replace(build_tool_context(AgentRuntimeConfig(cwd=self.root)), business_session_id='session-one')
                result = self.call(tool, args, restored)
                self.assertFalse(result.ok)
                self.assertIn('outcome is unknown', result.content)
                self.assertEqual(post.call_count, 1)

    def test_changed_uploaded_file_creates_a_new_operation(self):
        args = CASES[1][3]
        with patch('src.business_functions._post_uploaded_videos', side_effect=['before', 'after']) as post:
            first = self.payload(self.call('submit_video_processing', args))
            (self.root / 'demo.mp4').write_bytes(b'changed video contents')
            second = self.payload(self.call('submit_video_processing', args))
            self.assertNotEqual(first['idempotency_key'], second['idempotency_key'])
            self.assertEqual(post.call_count, 2)

    def test_concurrent_duplicate_submissions_send_once(self):
        with patch('src.business_functions._post_uploaded_videos', return_value='one-task') as post:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: self.call('submit_video_processing', CASES[1][3]), range(2)))
            payloads = [self.payload(r) for r in results]
            self.assertEqual(post.call_count, 1)
            self.assertEqual({p['task_id'] for p in payloads}, {'one-task'})

    def test_tool_schemas_do_not_expose_keys(self):
        for tool, _, _, _ in CASES:
            schema = self.registry[tool].parameters
            self.assertNotIn('idempotency_key', schema['properties'])
            self.assertIn('repeat_of_task_id', schema['properties'])
            self.assertNotIn('repeat_of_task_id', schema['required'])

    def test_agent_resume_reuses_business_operation_with_no_key_in_model_arguments(self):
        from src.agent_runtime import LocalCodingAgent
        from src.agent_types import ModelConfig
        from src.session_store import load_agent_session
        from tests.test_agent_runtime import FakeHTTPResponse

        def reply(message, finish):
            return FakeHTTPResponse({'choices': [{'message': message, 'finish_reason': finish}]})

        def submit(call_id):
            return reply({'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': call_id, 'type': 'function', 'function': {
                    'name': 'submit_video_processing', 'arguments': json.dumps(CASES[1][3]),
                },
            }]}, 'tool_calls')

        responses = [submit('call-one'), reply({'role': 'assistant', 'content': 'Submitted.'}, 'stop'),
                     submit('call-two'), reply({'role': 'assistant', 'content': 'Reused.'}, 'stop')]
        config = AgentRuntimeConfig(cwd=self.root, session_directory=self.root / 'sessions',
                                    scratchpad_root=self.root / 'scratchpad')
        model = ModelConfig(model='test-model', base_url='http://model.test/v1')
        with patch('src.openai_compat.request.urlopen', side_effect=responses), patch(
            'src.business_functions._post_uploaded_videos', return_value='runtime-task'
        ) as post:
            first_agent = LocalCodingAgent(model_config=model, runtime_config=config)
            first = first_agent.run('Label demo.mp4')
            self.assertEqual(first.final_output, 'Submitted.')
            stored = load_agent_session(first.session_id, directory=config.session_directory)
            resumed_agent = LocalCodingAgent(model_config=model, runtime_config=config)
            second = resumed_agent.resume('Continue the same labeling task', stored)
            self.assertEqual(second.final_output, 'Reused.')
            self.assertEqual(first.session_id, second.session_id)
            self.assertEqual(post.call_count, 1)


    def test_business_http_requests_never_include_internal_keys(self):
        import httpx

        cases = [
            (CASES[0][0], CASES[0][3], {'files'}),
            (CASES[1][0], CASES[1][3], {'files'}),
            (CASES[2][0], CASES[2][3], {'json'}),
            ('submit_video_analysis', {
                'scenario': 'fire_inspection',
                'video_ref': {'type': 'local_file', 'path': '/backend/demo.mp4'},
            }, {'params'}),
            ('submit_video_analysis', {
                'scenario': 'fire_inspection',
                'video_ref': {'type': 'cos_file', 'path': 'videos/demo.mp4'},
            }, {'params'}),
        ]
        for index, (tool, args, expected_fields) in enumerate(cases):
            with self.subTest(tool=tool, args=args), patch('src.business_functions.httpx.Client') as client:
                post = client.return_value.__enter__.return_value.post
                post.return_value = httpx.Response(
                    200, json={'task_id': f'http-task-{index}'},
                    request=httpx.Request('POST', 'http://business.test/submit'),
                )
                result = self.payload(self.call(tool, args))
                self.assertTrue(result['idempotency_key'])
                kwargs = post.call_args.kwargs
                self.assertEqual(set(kwargs), expected_fields)
                if 'json' in kwargs:
                    self.assertEqual(kwargs['json'], {
                        'scenario': 'fire_inspection', 'dataset_ref': 'fire-inspect-01',
                    })
                elif 'params' in kwargs:
                    field = 'filepath' if args['video_ref']['type'] == 'local_file' else 'cos_filepath'
                    self.assertEqual(kwargs['params'], {field: args['video_ref']['path']})
                else:
                    files = kwargs['files']
                    fields = set(files) if isinstance(files, dict) else {item[0] for item in files}
                    self.assertEqual(fields, {'file'})
