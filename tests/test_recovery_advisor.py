import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from recovery_advisor import advise


class RecoveryAdvisorTests(unittest.TestCase):
    def test_normal_work_is_direct(self):
        for evidence in ({}, {'implementation_requested': True},
                         {'implementation_requested': True, 'matching_failures': 1},
                         {'matching_failures': 8, 'planning_cycles_without_progress': 8}):
            with self.subTest(evidence=evidence):
                self.assertEqual(advise(evidence)['actions'], ['continue_direct'])

    def test_repeated_failure_changes_method_once(self):
        evidence = dict(implementation_requested=True, matching_failures=2)
        self.assertEqual(advise(evidence)['actions'], ['bounded_parent_recovery'])
        evidence.update(recovery_used=True, independent_work_available=True)
        self.assertEqual(advise(evidence)['actions'], ['park_packet_continue_independent_work'])
        evidence['independent_work_available'] = False
        self.assertEqual(advise(evidence)['actions'], ['report_unresolved_core_dependency'])

    def test_planning_loop_only_for_implementation(self):
        self.assertEqual(advise(dict(implementation_requested=True,
            planning_cycles_without_progress=2))['actions'], ['bounded_parent_recovery'])

    def test_conflicting_vision_prevents_implementation_advice(self):
        self.assertEqual(advise(dict(goal_conflict=True, implementation_requested=True,
            matching_failures=3, measured_regression=True, cleanup_requested=True))['actions'],
            ['resolve_product_conflict_with_user'])

    def test_explicit_and_observable_triggers(self):
        for flag, action in [('verification_missing', 'repair_smallest_verification_gap'),
                             ('measured_regression', 'measure_then_optimize_in_scope'),
                             ('optimization_requested', 'measure_then_optimize_in_scope'),
                             ('cleanup_requested', 'bounded_behavior_preserving_cleanup')]:
            with self.subTest(flag=flag):
                self.assertEqual(advise({'implementation_requested': True, flag: True})['actions'], [action])
                self.assertEqual(advise({flag: True})['actions'],
                    ['inspect_verification_gap_read_only'] if flag == 'verification_missing' else ['continue_direct'])

    def test_release_change_does_not_change_preferences(self):
        result = advise({'model_changed': True})
        self.assertEqual(result['actions'], ['recommend_compatibility_comparison_only'])
        self.assertFalse(result['changes_model_or_effort'])
        self.assertFalse(result['starts_workers'])
        self.assertFalse(result['grants_permission'])

    def test_gate_does_not_say_whole_goal_blocked(self):
        result = advise({'external_gate': True})
        self.assertEqual(result['actions'], ['request_specific_authority'])
        result = advise({'external_gate': True, 'independent_work_available': False})
        self.assertEqual(result['actions'], ['request_specific_authority'])
        result = advise({'external_gate': True, 'independent_work_available': True})
        self.assertEqual(result['actions'], ['request_specific_authority_continue_independent_work'])

    def test_invalid_types_and_unknown_fields_fail_closed(self):
        for evidence in ([], None, {'matching_failures': True}, {'matching_failures': -1},
                         {'matching_failures': 10001}, {'model_changed': 'false'},
                         {'run_command': 'anything'}, {'goal_conflict': None}):
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                advise(evidence)

    def test_actual_cli_valid_and_invalid_inputs(self):
        for raw, expected in [('{}', 0), ('{"implementation_requested":true,"matching_failures":2}', 0),
                              ('not json', 2), ('[]', 2), (' ' * 16385, 2)]:
            result = subprocess.run([sys.executable, str(ROOT/'scripts/recovery_advisor.py')],
                input=raw, text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, expected, result.stderr)
            if not expected:
                self.assertTrue(json.loads(result.stdout)['advisory_only'])


if __name__ == '__main__':
    unittest.main()
