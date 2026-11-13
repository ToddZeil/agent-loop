import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from scripts.seo_lite import SeoLiteRun, descendants, guard, spent, stop_reason, watch


class SeoBoundaries(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.control = {'run_dir': self.temp.name, 'deadline': 100, 'budget_usd': .30}
        self.ledger = Path(self.temp.name) / 'dataforseo-spend.json'

    def write(self, entries=(), reservations=()):
        self.ledger.write_text(json.dumps({'entries': entries, 'reservations': reservations}))

    def test_deadline_is_inclusive_and_not_extended_by_restart(self):
        self.assertIsNone(stop_reason(self.control, 99))
        self.assertEqual(stop_reason(self.control, 100), 'eight_hour_deadline')
        self.assertEqual(stop_reason(self.control, 101), 'eight_hour_deadline')

    def test_reservations_and_uncertain_charges_stop_run(self):
        self.write([{'costUsd': .296}], [{'estimatedCostUsd': .004}])
        self.assertAlmostEqual(spent(self.ledger), .30)
        self.assertEqual(stop_reason(self.control, 1), 'dataforseo_budget')

    def test_historical_run_costs_never_expire(self):
        self.write([{'costUsd': .30, 'at': '2000-01-01'}])
        self.assertEqual(stop_reason(self.control, 1), 'dataforseo_budget')

    def test_disk_guard_stops_before_storage_is_exhausted(self):
        self.control['minimum_free_bytes'] = 2048
        with patch('scripts.seo_lite.shutil.disk_usage') as usage:
            usage.return_value.free = 2047
            self.assertEqual(stop_reason(self.control, 1), 'low_disk_space')
            usage.return_value.free = 2048
            self.assertIsNone(stop_reason(self.control, 1))

    def test_extended_run_continues_free_work_after_paid_budget(self):
        self.write([{'costUsd': .30}])
        self.control.update(stop_on_budget=False, deadline_reason='six_pm_deadline')
        self.assertIsNone(stop_reason(self.control, 99))
        self.assertEqual(stop_reason(self.control, 100), 'six_pm_deadline')

    def test_provider_preference_falls_back_and_returns_after_reset(self):
        usage = {'claude': 90, 'codex': 4}
        run = SeoLiteRun(self.temp.name, 'test', invoke=lambda *a: None,
                         limits=lambda p: {'usedPercent': usage[p]})
        run.dir.mkdir(parents=True)
        run.save('config.json', {'prefer_claude': True})
        state = {'holds': {}}
        self.assertEqual(run.available(['codex', 'claude'], state), ['claude', 'codex'])
        usage['claude'] = 100
        run._limits_cache.clear()
        self.assertEqual(run.available(['claude', 'codex'], state), ['codex'])
        usage['claude'] = 90
        run._limits_cache.clear()
        self.assertEqual(run.available(['claude', 'codex'], state), ['claude', 'codex'])

    def test_dispatch_enforces_preferred_provider_and_standing_instructions(self):
        run = SeoLiteRun(self.temp.name, 'test')
        run.dir.mkdir(parents=True)
        run.save('config.json', {'prefer_claude': True, 'workers': ['claude', 'codex']})
        run.path('goal.md').write_text('Use local review branches and preserve the spending cap.')
        with patch.object(run, 'available', return_value=['claude', 'codex']), \
             patch('loop.lite.LiteRun.dispatch', side_effect=lambda state, action: action):
            action = run.dispatch({}, {'provider': 'codex', 'prompt': 'Improve Japanese content.'})
        self.assertEqual(action['provider'], 'claude')
        self.assertIn('Japanese', action['prompt'])
        self.assertIn('preserve the spending cap', action['prompt'])

    def test_corrupted_ledger_fails_closed(self):
        self.ledger.write_text('broken')
        with self.assertRaises(ValueError):
            stop_reason(self.control, 1)

    def test_descendants_include_new_sessions_but_not_unrelated_runs(self):
        self.assertEqual(descendants(10, [(11, 10), (12, 11), (20, 1), (21, 20)]), {10, 11, 12})

    def test_guard_rejects_changes_to_tests(self):
        control_path = Path(self.temp.name) / 'control.json'
        control_path.write_text(json.dumps({**self.control, 'worktree': self.temp.name,
                                           'base_sha': 'base'}))
        with patch('scripts.seo_lite.git', side_effect=['tests/check.py', '', '']):
            with self.assertRaisesRegex(ValueError, 'tests/check.py'):
                guard(control_path)

    def test_watchdog_stops_active_descendants_at_deadline(self):
        control_path = Path(self.temp.name) / 'control.json'
        control_path.write_text(json.dumps(self.control))
        awake, worker = MagicMock(), MagicMock()
        worker.pid = 200
        worker.poll.return_value = None
        with patch('scripts.seo_lite.subprocess.Popen', side_effect=[awake, worker]), \
             patch('scripts.seo_lite.process_tree', return_value={200, 201, 202}), \
             patch('scripts.seo_lite.terminate') as terminate, \
             patch('scripts.seo_lite.stop_reason',
                   side_effect=[None, None, 'eight_hour_deadline']):
            # An active runner cannot exit until the watchdog terminates it.
            worker.wait.side_effect = lambda: self.assertTrue(terminate.called)
            watch(control_path)
        terminate.assert_called_once_with({200, 201, 202})
        worker.wait.assert_called_once()
        awake.terminate.assert_called_once()
        status = json.loads((Path(self.temp.name) / 'bounded-status.json').read_text())
        self.assertEqual(status['reason'], 'eight_hour_deadline')
        self.assertEqual(status['status'], 'stopped')

    def test_watchdog_restarts_an_early_runner_exit(self):
        control_path = Path(self.temp.name) / 'control.json'
        control_path.write_text(json.dumps(self.control))
        awake, worker = MagicMock(), MagicMock()
        worker.pid = 200
        worker.poll.return_value = 1
        worker.wait.return_value = 1
        with patch('scripts.seo_lite.subprocess.Popen', side_effect=[awake, worker]), \
             patch('scripts.seo_lite.process_tree', return_value={200}), \
             patch('scripts.seo_lite.terminate') as terminate, \
             patch('scripts.seo_lite.time.sleep') as sleep, \
             patch('scripts.seo_lite.stop_reason',
                   side_effect=[None, None, None, 'eight_hour_deadline', 'eight_hour_deadline']):
            watch(control_path)
        worker.wait.assert_called_once()
        terminate.assert_not_called()
        sleep.assert_called_once()
        status = json.loads((Path(self.temp.name) / 'bounded-status.json').read_text())
        self.assertEqual(status['restart_count'], 1)
        self.assertEqual(status['reason'], 'eight_hour_deadline')


if __name__ == '__main__':
    unittest.main()
