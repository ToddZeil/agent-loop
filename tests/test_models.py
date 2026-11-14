import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from loop.models import catalog, remember


class ModelCatalogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.home = root / 'web'; self.home.mkdir()
        self.cache = root / 'codex'; self.cache.mkdir()
        patch = mock.patch.dict(os.environ, {'CODEX_HOME': str(self.cache)})
        patch.start(); self.addCleanup(patch.stop)

    def write(self, models):
        (self.cache / 'models_cache.json').write_text(json.dumps({'fetched_at': 'today', 'models': models}))

    def row(self, name, visibility='list'):
        return {'slug': name, 'display_name': name.upper(), 'visibility': visibility,
                'supported_reasoning_levels': [{'effort': 'high'}]}

    def test_claude_aliases_are_available_without_discovery_and_survive_refresh(self):
        for _ in range(2):
            result = catalog(self.home)['claude']
            self.assertEqual([m['id'] for m in result['models']], ['sonnet', 'opus', 'haiku'])
            self.assertEqual(result['source'], 'Claude Code model aliases')

    def test_smaller_cache_retains_discovered_choices_and_effort(self):
        self.write([self.row('model-a'), self.row('model-b')])
        catalog(self.home)
        self.write([self.row('model-b')])
        result = catalog(self.home)['codex']['models']
        by_id = {m['id']: m for m in result}
        self.assertEqual(set(by_id), {'model-a', 'model-b'})
        self.assertTrue(by_id['model-a']['previously_seen'])
        self.assertEqual(by_id['model-a']['efforts'], ['high'])
        self.assertFalse(by_id['model-b']['previously_seen'])
        # Repeated refreshes must not gradually discard retained entries.
        self.assertEqual(len(catalog(self.home)['codex']['models']), 2)

    def test_explicitly_hidden_models_are_removed(self):
        self.write([self.row('model-a')]); catalog(self.home)
        self.write([self.row('model-a', 'hide')])
        self.assertEqual(catalog(self.home)['codex']['models'], [])

    def test_missing_or_invalid_cache_preserves_previous_choices(self):
        self.write([self.row('model-a')]); catalog(self.home)
        (self.cache / 'models_cache.json').write_text('not json')
        self.assertEqual(catalog(self.home)['codex']['models'][0]['id'], 'model-a')
        (self.cache / 'models_cache.json').unlink()
        self.assertEqual(catalog(self.home)['codex']['models'][0]['id'], 'model-a')

    def test_custom_choices_persist_and_upgrade_when_discovered(self):
        remember(self.home, 'codex', 'model-a')
        remember(self.home, 'claude', 'custom-claude')
        self.write([self.row('model-a')])
        result = catalog(self.home)
        self.assertFalse(result['codex']['models'][0]['custom'])
        self.assertIn('custom-claude', [m['id'] for m in result['claude']['models']])
